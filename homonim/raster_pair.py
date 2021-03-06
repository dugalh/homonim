"""
    Homonim: Correction of aerial and satellite imagery to surface reflectance
    Copyright (C) 2021 Dugal Harris
    Email: dugalh@gmail.com

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as
    published by the Free Software Foundation, either version 3 of the
    License, or any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
import logging
import pathlib
import threading
from contextlib import ExitStack
from itertools import product
from typing import Tuple, NamedTuple, Union, Iterable

import numpy as np
import rasterio
import rasterio as rio
from rasterio.enums import MaskFlags
from rasterio.vrt import WarpedVRT
from rasterio.warp import Resampling
from rasterio.windows import Window
from tqdm.contrib.logging import logging_redirect_tqdm

from homonim import errors, utils
from homonim.enums import ProcCrs
from homonim.raster_array import RasterArray

logger = logging.getLogger(__name__)


class BlockPair(NamedTuple):
    """ A set of matching block windows for a source-reference image pair. """
    band_i: int
    """ Band index (0 based). """
    src_in_block: Window
    """ Overlapping / input source window. """
    ref_in_block: Window
    """ Overlapping / input reference window. """
    src_out_block: Window
    """ Non-overlapping / output source window. """
    ref_out_block: Window
    """ Non-overlapping / output reference window. """
    outer: bool
    """ True if any part of the source blocks touch the source image boundary, otherwise False. """


class RasterPairReader:

    def __init__(
        self, src_filename: Union[str, pathlib.Path], ref_filename: Union[str, pathlib.Path],
        proc_crs: ProcCrs = ProcCrs.auto
    ):
        """
        Class for reading matching, and optionally overlapping, blocks from a source and reference image pair.

        Parameters
        ----------
        src_filename: str, pathlib.Path
            Path to the source image file.
        ref_filename: str, pathlib.Path
            Path to the reference image file.  The extents of this image should cover the source with at least a 2
            pixel border.  The reference image should have at least as many bands as the source, and the
            ordering of the source and reference bands should match.
        proc_crs: homonim.enums.ProcCrs, optional
            :class:`~homonim.enums.ProcCrs` instance specifying which of the source/reference image spaces will be
            used for processing.  For most use cases, including typical surface reflectance correction,
            it can be left as the default of :attr:`~homonim.enums.ProcCrs.auto`. In this case it will be resolved to
            refer to the lowest resolution of the source and reference image CRS's.
        """
        self._src_filename = pathlib.Path(src_filename)
        self._ref_filename = pathlib.Path(ref_filename)
        proc_crs = ProcCrs(proc_crs)
        self._proc_crs = proc_crs

        self._env = None
        self._src_lock = threading.Lock()
        self._ref_lock = threading.Lock()
        self._src_im = None
        self._ref_im = None
        self._stack = None
        self._init_image_pair()

    @property
    def src_im(self) -> rasterio.DatasetReader:
        """ Source rasterio dataset. """
        self._assert_open()
        return self._src_im

    @property
    def ref_im(self) -> rasterio.DatasetReader:
        """ Reference rasterio dataset. """
        self._assert_open()
        return self._ref_im

    @property
    def src_bands(self) -> Tuple[int, ...]:
        """ Source non-alpha band indices (1-based). """
        return self._src_bands

    @property
    def ref_bands(self) -> Tuple[int, ...]:
        """ Reference non-alpha band indices (1-based). """
        return self._ref_bands

    @property
    def proc_crs(self) -> ProcCrs:
        """ Which of the source and reference image CRS's will be used for processing. """
        return self._proc_crs

    @property
    def closed(self) -> bool:
        """ True if both source and reference images are closed, otherwise False. """
        return not self._src_im or not self._ref_im or self._src_im.closed or self._ref_im.closed

    @staticmethod
    def _validate_image(im: rasterio.DatasetReader):
        """ Validate an open rasterio dataset for use as a source or reference image. """

        try:
            _ = im.read(1, window=im.block_window(1, 0, 0))
        except Exception as ex:
            if 'compress' in im.profile and im.profile['compress'] == 'jpeg':  # assume it is a 12bit JPEG
                raise errors.UnsupportedImageError(
                    f'Could not read image {im.name}.  JPEG compression with NBITS==12 is not supported, '
                    f'you probably need to recompress this image.'
                )
            else:
                raise ex

        # warn if there is no nodata or associated mask
        is_masked = any([MaskFlags.all_valid not in im.mask_flag_enums[bi] for bi in range(im.count)])
        if im.nodata is None and not is_masked:
            logger.warning(
                f'{im.name} has no mask or nodata value, any invalid pixels should be masked before processing.'
            )

    @staticmethod
    def _validate_image_pair(src_im: rasterio.DatasetReader, ref_im: rasterio.DatasetReader):
        """ Validate a pair of rasterio datasets for use as a source-reference image pair. """

        for im in (src_im, ref_im):
            RasterPairReader._validate_image(im)
        # check reference image extents cover the source
        if not utils.covers_bounds(ref_im, src_im):
            raise errors.ImageContentError(f'Reference extent does not cover source image')

        # retrieve non-alpha bands
        src_bands = utils.get_nonalpha_bands(src_im)
        ref_bands = utils.get_nonalpha_bands(ref_im)

        # check reference has enough bands
        if len(src_bands) > len(ref_bands):
            raise errors.ImageContentError(
                f'Reference ({ref_im.name}) has fewer non-alpha bands than source ({src_im.name}).'
            )

        # warn if source and reference band counts don't match
        if len(src_bands) != len(ref_bands):
            logger.warning(
                f'Source and reference image non-alpha band counts don`t match. Using the first {len(src_bands)} '
                f'non-alpha bands of reference.'
            )

        # warn if the source and reference are not in the same CRS
        if src_im.crs.to_proj4() != ref_im.crs.to_proj4():
            logger.warning(f'Source and reference image pair are not in the same CRS: {src_im.name} and {ref_im.name}')

    @staticmethod
    def _resolve_proc_crs(
        src_im: rasterio.DatasetReader, ref_im: rasterio.DatasetReader, proc_crs: ProcCrs = ProcCrs.auto
    ) -> ProcCrs:
        """
        Resolve a :class:`~homonim.enums.ProcCrs` instance.
        The :class:`~homonim.enums.ProcCrs` instance is resolved from :attr:`~homonim.enums.ProcCrs.auto` to the lowest
        resolution CRS of the supplied source and reference images.  If the :class:`~homonim.enums.ProcCrs` instance is
        already resolved, and doesn't correspond to the lowest resolution CRS, then a warning is issued.
        """
        # compare source and reference resolutions
        src_pixel_smaller = np.prod(np.abs(src_im.res)) <= np.prod(np.abs(ref_im.res))
        cmp_str = 'smaller' if src_pixel_smaller else 'larger'
        if proc_crs == ProcCrs.auto:
            # set proc_crs to the lowest resolution of the source and reference images
            proc_crs = ProcCrs.ref if src_pixel_smaller else ProcCrs.src
            logger.debug(
                f'Source pixel size {np.round(src_im.res, decimals=3)} is {cmp_str} than the reference '
                f'{np.round(ref_im.res, decimals=3)}. Using proc_crs=`{proc_crs}`.'
            )
        elif (
            (proc_crs == ProcCrs.src and src_pixel_smaller) or
            (proc_crs == ProcCrs.ref and not src_pixel_smaller)
        ):  # yapf: disable
            # warn if the proc_crs value does not correspond to the lowest resolution of the source and
            # reference images
            rec_crs_str = ProcCrs.ref if src_pixel_smaller else ProcCrs.src
            logger.warning(
                f'proc_crs={rec_crs_str} is recommended when the source pixel size is {cmp_str} than the reference.'
            )
        return proc_crs

    @staticmethod
    def resolve_proc_crs(
        src_filename: pathlib.Path, ref_filename: pathlib.Path, proc_crs: ProcCrs = ProcCrs.auto
    ) -> ProcCrs:
        """
        Resolve a :class:`~homonim.enums.ProcCrs` instance.

        The :class:`~homonim.enums.ProcCrs` instance is resolved from :attr:`~homonim.enums.ProcCrs.auto` to the lowest
        resolution CRS of the supplied source and reference images.  If the :class:`~homonim.enums.ProcCrs` instance is
        already resolved, and doesn't correspond to the lowest resolution CRS, then a warning is issued.

        Parameters
        ----------
        src_filename: str, pathlib.Path
            Path to the source image file.
        ref_filename: str, pathlib.Path
            Path to the reference image file.
        proc_crs: homonim.enums.ProcCrs, optional
            :class:`~homonim.enums.ProcCrs` instance to resolve.

        Returns
        -------
        ProcCrs
            :class:`~homonim.enums.ProcCrs` instance resolved to either :attr:`~homonim.enums.ProcCrs.src` or
            :attr:`~homonim.enums.ProcCrs.ref`.
        """
        with rio.open(src_filename, 'r') as src_im, rio.open(ref_filename, 'r') as _ref_im:
            with (
                WarpedVRT(_ref_im, crs=src_im.crs, resampling=Resampling.bilinear)
                if src_im.crs.to_proj4() != _ref_im.crs.to_proj4() else _ref_im
            ) as ref_im:  # yapf: disable
                return RasterPairReader._resolve_proc_crs(src_im, ref_im, proc_crs=proc_crs)

    def _auto_block_shape(self, max_block_mem: float = np.inf) -> Tuple[int, int]:
        """ Find a block shape that satisfies max_block_mem. """

        proc_win = self._ref_win if self.proc_crs == ProcCrs.ref else self._src_win
        # adjust max_block_mem to represent the size of a block in the highest resolution image, but scaled to the
        # equivalent in proc_crs.
        src_pix_area = np.product(np.abs(self._src_im.res))
        ref_pix_area = np.product(np.abs(self._ref_im.res))
        if self.proc_crs == ProcCrs.ref:
            mem_scale = src_pix_area / ref_pix_area if ref_pix_area > src_pix_area else 1.
        elif self.proc_crs == ProcCrs.src:
            mem_scale = 1. if ref_pix_area > src_pix_area else ref_pix_area / src_pix_area
        else:
            raise ValueError("'proc_crs' has not been resolved - the raster pair must be opened first.")
        max_block_mem = max_block_mem * mem_scale if max_block_mem > 0 else np.inf

        max_block_mem *= 2 ** 20  # convert MB to bytes
        dtype_size = np.dtype(RasterArray.default_dtype).itemsize  # the size of the RasterArray data type

        # set the starting block_shape to correspond to the entire window
        block_shape = np.array((proc_win.height, proc_win.width)).astype('float')

        # keep halving the block_shape along the longest dimension until it satisfies max_block_mem
        while (np.product(block_shape) * dtype_size) > max_block_mem:
            div_dim = np.argmax(block_shape)
            block_shape[div_dim] /= 2

        if np.any(block_shape < (1, 1)):
            raise errors.BlockSizeError(f"The auto block shape is smaller than a pixel.  Increase 'max_block_mem'.")

        block_shape = np.ceil(block_shape).astype('int')
        logger.debug(
            f'Auto block shape: {block_shape}, of image shape: {[proc_win.height, proc_win.width]}'
            f' ({self.proc_crs.name} pixels)'
        )

        # warn if the block shape in the highest res image is less than a typical tile
        if np.any(block_shape / mem_scale < (256, 256)) and np.any(block_shape < (proc_win.height, proc_win.width)):
            logger.warning(
                f'The auto block shape is small: {block_shape}.  Increase `max_block_mem` to improve processing times.'
            )
        return tuple(block_shape)

    def _init_image_pair(self):
        """ Prepare the raster pair for reading. """
        # check and resolve proc_crs before opening
        self._proc_crs = self.resolve_proc_crs(self._src_filename, self._ref_filename, proc_crs=self._proc_crs)
        self.open()
        try:
            self._validate_image_pair(self._src_im, self._ref_im)
            # get non-alpha band indices for reading
            self._src_bands = utils.get_nonalpha_bands(self._src_im)
            logger.debug(f'{self._src_filename.name} non-alpha bands: {self._src_bands}')
            self._ref_bands = utils.get_nonalpha_bands(self._ref_im)
            logger.debug(f'{self._ref_filename.name} non-alpha bands: {self._ref_bands}')

            # create image windows that allow re-projections between source and reference without loss of data
            self._ref_win = utils.expand_window_to_grid(self._ref_im.window(*self._src_im.bounds))
            self._src_win = utils.expand_window_to_grid(self._src_im.window(*self._ref_im.window_bounds(self._ref_win)))

        finally:
            self.close()

    def _assert_open(self):
        """ Raise an IoError if the source and reference images are not open. """
        if self.closed:
            raise errors.IoError(
                f'The raster pair has not been opened: {self._src_filename.name} and {self._ref_filename.name}'
            )

    def open(self):
        """ Open the source and reference images for reading. """
        self._src_im = rio.open(self._src_filename, 'r')
        self._ref_im = rio.open(self._ref_filename, 'r')

        # It is essential that the source and reference are in the same CRS so that rectangular regions of valid
        # data in one will re-project to rectangular regions of valid data in the other.
        if self._src_im.crs.to_proj4() != self._ref_im.crs.to_proj4():
            # open the image pair in the same CRS, re-projecting the proc_crs (usually lower resolution) image into the
            # CRS of the other
            if self.proc_crs == ProcCrs.src:
                self._src_im = WarpedVRT(self._src_im, crs=self._ref_im.crs, resampling=Resampling.bilinear)
            else:
                self._ref_im = WarpedVRT(self._ref_im, crs=self._src_im.crs, resampling=Resampling.bilinear)

    def close(self):
        """ Close the source and reference image datasets. """
        self._src_im.close()
        self._ref_im.close()

    def __enter__(self):
        self._stack = ExitStack()
        # TODO: if the below env settings apply to reads, then might setting gdal cache size to e.g. block size help?
        self._stack.enter_context(rio.Env(GDAL_NUM_THREADS='ALL_CPUs', GTIFF_FORCE_RGBA=False))
        self._stack.enter_context(logging_redirect_tqdm([logging.getLogger(__package__)]))
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        self._stack.__exit__(exc_type, exc_val, exc_tb)

    def read(self, block_pair: BlockPair) -> Tuple[RasterArray, RasterArray]:
        """
        Thread-safe read of a matching pair of source and reference image blocks.

        Parameters
        ----------
        block_pair: BlockPair
            :class:`BlockPair` named tuple, specifying the :attr:`BlockPair.src_in_block` source window, and
            :attr:`BlockPair.ref_in_block` reference window to read.  This :class:`BlockPair` instance is
            typically obtained from :meth:`block_pairs`.

        Returns
        -------
        src_ra: RasterArray
            Source image block wrapped in a RasterArray.
        ref_ra: RasterArray
            Reference image block wrapped in a RasterArray.
        """
        self._assert_open()
        # TODO: are these reads done in the context gdal environment?  or might we speed up by entering another
        #  environment here with NUM_THREADS=ALL_CPUS
        with self._src_lock:
            src_ra = RasterArray.from_rio_dataset(
                self._src_im, indexes=self._src_bands[block_pair.band_i], window=block_pair.src_in_block
            )
        with self._ref_lock:
            ref_ra = RasterArray.from_rio_dataset(
                self._ref_im, indexes=self._ref_bands[block_pair.band_i], window=block_pair.ref_in_block
            )
        return src_ra, ref_ra

    def block_pairs(self, overlap: Tuple[int, int] = (0, 0), max_block_mem: float = np.inf) -> Iterable[BlockPair]:
        """
        Iterator over paired source-reference image blocks.

        Parameters
        ----------
        overlap: tuple of int
            Block overlap (rows, columns) in pixels of the :attr:`proc_crs` image.
        max_block_mem: float
            Maximum allowable block size in MB. The image is divided into 2\\ :sup:`n` blocks with n the smallest number
            where ``max_block_mem`` is satisfied.  If ``max_block_mem`` is `float('inf')`, the block shape will be set
            to encompass the full extent of the source image.

        Yields
        -------
        BlockPair
            Named tuple specifying the overlapping (``*_in_block``), and non-overlapping (``*_out_block``) image blocks.
        """
        self._assert_open()
        # generate the auto block shape for reading
        overlap = np.array(overlap).astype('int')
        block_shape = self._auto_block_shape(max_block_mem=max_block_mem)
        if np.any(block_shape <= overlap):
            raise errors.BlockSizeError(f'The auto block shape is smaller than the overlap.  Increase `max_block_mem`.')
        logger.debug(f'Block overlap: {overlap} ({self.proc_crs.name} pixels)')

        # initialise block formation variables
        # blocks are first formed in proc_crs, then transformed to the 'other' image crs, so here we assign the src/ref
        # windows etc. to proc_* equivalents
        if self.proc_crs == ProcCrs.ref:
            proc_win, proc_im, other_im = (self._ref_win, self._ref_im, self._src_im)
        else:
            proc_win, proc_im, other_im = (self._src_win, self._src_im, self._ref_im)

        proc_win_ul = np.array((proc_win.row_off, proc_win.col_off))
        proc_win_br = np.array((proc_win.height + proc_win.row_off, proc_win.width + proc_win.col_off))

        # Outer loop over bands so that all blocks in a band are yielded consecutively - this is fastest for
        # reading band interleaved images.
        for band_i in range(len(self._src_bands)):
            # Inner loop over the upper left corner row, col for each overlapping block
            ul_row_range = range(
                proc_win.row_off - overlap[0], proc_win.row_off + proc_win.height - overlap[0], block_shape[0]
            )
            ul_col_range = range(
                proc_win.col_off - overlap[1], proc_win.col_off + proc_win.width - overlap[1], block_shape[1]
            )
            for ul_row, ul_col in product(ul_row_range, ul_col_range):
                # find UL and BR corners for overlapping block in proc space
                ul = np.array((ul_row, ul_col))
                br = ul + block_shape + (2 * overlap)
                # limit block extents to image window extents
                in_ul = np.fmax(ul, proc_win_ul)
                in_br = np.fmin(br, proc_win_br)
                # find UL and BR corners for non-overlapping block in proc space
                out_ul = np.fmax(ul + overlap, proc_win_ul)
                out_br = np.fmin(br - overlap, proc_win_br)
                # block touches image boundary?
                outer = np.any(in_ul <= proc_win_ul) or np.any(in_br >= proc_win_br)

                # Create rasterio windows corresponding to above block corners.
                # Note:
                # - Consecutive proc_in_block's will overlap by exactly ``overlap``.
                # - Consecutive proc_out_block's will be exactly adjacent.
                proc_in_block = Window(*in_ul[::-1], *np.subtract(in_br, in_ul)[::-1])
                proc_out_block = Window(*out_ul[::-1], *np.subtract(out_br, out_ul)[::-1])

                # Create equivalent rasterio windows in 'other' space.
                # Note:
                # - other_in_block boundaries are expanded to ensure that re-projecting between source/reference
                #   CRSs does not mask valid *_out_block pixels.  This means that consecutive other_in_blocks may
                #   overlap by more than ``overlap``.
                # - consecutive other_out_block's may overlap by a pixel.
                other_in_block = utils.expand_window_to_grid(other_im.window(*proc_im.window_bounds(proc_in_block)))
                other_out_block = utils.round_window_to_grid(other_im.window(*proc_im.window_bounds(proc_out_block)))

                # create the BlockPair named tuple, assigning 'proc' and 'other' back to 'src' and 'ref' for passing to
                # read()
                if self.proc_crs == ProcCrs.ref:
                    block_pair = BlockPair(
                        band_i, other_in_block, proc_in_block, other_out_block, proc_out_block, outer
                    )
                else:
                    block_pair = BlockPair(
                        band_i, proc_in_block, other_in_block, proc_out_block, other_out_block, outer
                    )
                yield block_pair
