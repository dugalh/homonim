"""
    Homonim: Correction of aerial and satellite imagery to surface relfectance
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
import multiprocessing
import threading
from concurrent import futures
from contextlib import ExitStack, contextmanager
from typing import Tuple, Dict, Union
from pathlib import Path

import numpy as np
import rasterio as rio
import rasterio.io
from rasterio.enums import Resampling
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

from homonim import utils
from homonim.enums import Method, ProcCrs
from homonim.errors import IoError
from homonim.kernel_model import KernelModel, RefSpaceModel, SrcSpaceModel
from homonim.raster_array import RasterArray
from homonim.raster_pair import RasterPairReader, BlockPair

logger = logging.getLogger(__name__)


class RasterFuse(RasterPairReader):
    """
    Class for homogenising ('fusing') a source image with a reference image.
    """

    # default configuration dicts
    default_fuse_config = dict(param_image=False, threads=multiprocessing.cpu_count(), max_block_mem=100)
    # TODO: rename fuse_config and any other homo variables to keep with 'correction' terminology
    # TODO: rethink these config dicts... maybe something like cloup settings
    def __init__(
        self, src_filename: Union[Path, str], ref_filename: Union[Path, str], proc_crs: ProcCrs = ProcCrs.auto,
    ):
        """
        Create a RasterFuse class.

        Parameters
        ----------
        src_filename: str, Path
            Path to the source image file.
        ref_filename: str, Path
            Path to the reference image file.  The extents of this image should cover src_filename with at least a 2
            pixel boundary, and it should have at least as many bands as src_filename.  The ordering of the bands
            in src_filename and ref_filename should match.
        proc_crs: homonim.enums.ProcCrs, optional
            The initial proc_crs setting, specifying which of the source/reference image spaces should be used for
            parameter estimation .  If proc_crs=ProcCrs.auto (recommended), the lowest resolution image space will be
            used. [default: ProcCrs.auto]
        """
        # TODO: refactor so that process() takes an output filename, and kernel model settings.  Then the context
        #  enter only opens the source and ref files.  The corrected & param file are created and opened in process. (?)
        RasterPairReader.__init__(self, src_filename, ref_filename, proc_crs=proc_crs)

        # TODO: delete self._model_config and use self._model.param_image

        # initialise other variables
        self._write_lock = threading.Lock()
        self._param_lock = threading.Lock()
        self._out_im = None
        self._param_im = None

    @property
    def method(self) -> Method:
        """ Correction method. """
        return self._method

    @property
    def kernel_shape(self) -> Tuple[int, int]:
        """ Kernel shape. """
        return tuple(self._kernel_shape)

    # @property
    # def closed(self) -> bool:
    #     """ True when the RasterFuse images are closed, otherwise False. """
    #     return not self._out_im or self._out_im.closed or RasterPairReader.closed(self)
    # TODO: get rid of *filename, kernel_shape etc properties.
    @property
    def homo_filename(self) -> Path:
        """ Path to the corrected image file. """
        return self._homo_filename

    @property
    def param_filename(self) -> Path:
        """ Path to the parameter image file. """
        return self._param_filename if self._config['param_image'] else None

    @staticmethod
    def create_out_profile(
        driver: str = 'GTiff',
        dtype: str = RasterArray.default_dtype,
        nodata: float = RasterArray.default_nodata,
        creation_options: Dict = dict(
            tiled=True, blockxsize=512, blockysize=512, compress='deflate', interleave='band', photometric=None
        )
    )->Dict: # yapf: disable
        """
        Utility method to create a rasterio image profile for the output image(s) that can be passed to
        :meth:`RasterFuse.__init__`.  Without arguments, the default profile values are returned.

        Parameters
        ----------
        driver: str, optional
            Output format driver.
        dtype: str, optional
            Output image data type.
        nodata: float, optional
            Output image nodata value.
        creation_options: dict, optional
            Driver specific creation options.  See the GDAL documentation for more information.

        Returns
        -------
        dict
            rasterio image profile.
        """
        return dict(driver=driver, dtype=dtype, nodata=nodata, creation_options=creation_options)

    def _init_out_filenames(self, out_path: Path, overwrite: bool = True):
        """ Set up the corrected and parameter image filenames. """
        # TODO: if we are keeping homo_filename as an attr at all, rename it to out_filename or fuse_filename
        out_path = Path(out_path)
        if out_path.is_dir():
            # create a filename for the corrected file in the provided directory
            post_fix = utils.create_out_postfix(
                self.proc_crs, self._method, self.kernel_shape, self._out_profile['driver']
            )
            self._homo_filename = out_path.joinpath(self._src_filename.stem + post_fix)
        else:
            self._homo_filename = out_path

        self._param_filename = utils.create_param_filename(self._homo_filename)

        if not overwrite and self._homo_filename.exists():
            raise FileExistsError(
                f'Corrected image file exists and won\'t be overwritten without the `overwrite` option: '
                f'{self._homo_filename}'
            )
        if not overwrite and self._config['param_image'] and self._param_filename.exists():
            raise FileExistsError(
                f'Parameter image file exists and won\'t be overwritten without the `overwrite` option: '
                f'{self._param_filename}'
            )

    def _create_out_profile(self) -> dict:
        """ Create an output image rasterio profile from the source image profile and output configuration. """
        # out_profile = self._combine_with_config(self.src_im.profile)
        out_profile = utils.combine_profiles(self.src_im.profile, self._out_profile)
        out_profile['count'] = len(self.src_bands)
        return out_profile

    def _create_param_profile(self) -> dict:
        """ Create a debug image rasterio profile from the proc_crs image and configuration. """
        if self.proc_crs == ProcCrs.ref:
            init_profile = self.ref_im.profile
        else:
            init_profile = self.src_im.profile
        # param_profile = self._combine_with_config(init_profile)
        param_profile = utils.combine_profiles(init_profile, self._out_profile)
        # force dtype and nodata to defaults
        param_profile.update(
            dtype=RasterArray.default_dtype, count=len(self.src_bands) * 3,
            nodata=RasterArray.default_nodata
        )
        return param_profile

    def _set_metadata(self, im: rasterio.io.DatasetWriter):
        """ Helper function to copy the RasterFuse configuration info to an image (GeoTiff) file. """
        # TODO: rather than keep copies of e.g. method and kernel_shape, have them as properties of the actual class
        #  that uses them (KernelModel) in this case, and retrieve them via the e.g. self._kernel_model attr
        # TODO: auto create meta keys from self._model config.
        model_config = {f'FUSE_MODEL_{k.upper()}': v for k, v in self._model.config.items()}
        meta_dict = dict(
            FUSE_SRC_FILE=self._src_filename.name, FUSE_REF_FILE=self._ref_filename.name,
            FUSE_PROC_CRS=self.proc_crs.name, FUSE_METHOD=self._method.name, FUSE_KERNEL_SHAPE=self.kernel_shape,
            FUSE_MAX_BLOCK_MEM=str(self._config['max_block_mem']), FUSE_THREADS=str(self._config['threads']),
            **model_config,
        )
        im.update_tags(**meta_dict)

    def _set_homo_metadata(self, im):
        """
        Copy useful metadata to a corrected image (GeoTIFF) file.

        Parameters
        ----------
        im: rasterio.io.DatasetWriter
            An open rasterio dataset to write the metadata to.
        """
        self._assert_open()
        self._set_metadata(im)
        for bi in range(0, min(im.count, len(self.ref_bands))):
            ref_bi = self.ref_bands[bi]
            ref_meta_dict = self.ref_im.tags(ref_bi)
            homo_meta_dict = {k: v for k, v in ref_meta_dict.items() if k in ['ABBREV', 'ID', 'NAME']}
            im.set_band_description(bi + 1, self.ref_im.descriptions[ref_bi - 1])
            im.update_tags(bi + 1, **homo_meta_dict)

    def _set_param_metadata(self, im):
        """
        Copy useful metadata to a parameter image (GeoTIFF) dataset.

        Parameters
        ----------
        im: rasterio.io.DatasetWriter
            An open rasterio dataset to write the metadata to.
        """
        # Use reference file band descriptions to make debug image band descriptions
        self._assert_open()
        self._set_metadata(im)
        num_src_bands = len(self.src_bands)
        for bi in range(0, num_src_bands):
            ref_bi = self.ref_bands[bi]
            ref_descr = self.ref_im.descriptions[ref_bi - 1] or f'B{ref_bi}'
            ref_meta_dict = self.ref_im.tags(ref_bi)
            for param_i, param_name in zip(range(bi, im.count, num_src_bands), ['GAIN', 'OFFSET', 'R2']):
                im.set_band_description(param_i + 1, f'{ref_descr}_{param_name}')
                param_meta_dict = {
                    k: f'{v.upper()} {param_name}' for k, v in ref_meta_dict.items() if k in ['ABBREV', 'ID', 'NAME']
                }
                im.update_tags(param_i + 1, **param_meta_dict)

    def _open(self):
        """ Open the source and reference images for reading, and output image(s) for writing. """
        RasterPairReader.open(self)
        self._out_im = rio.open(self._homo_filename, 'w', **self._create_out_profile())
        self._set_homo_metadata(self._out_im)

        # TODO: since param_image is the only (?) item in _config that is used in RasterFuse, rather just have a
        #  self.param_image attr
        if self._config['param_image']:
            self._param_im = rio.open(self._param_filename, 'w', **self._create_param_profile())
            self._set_param_metadata(self._param_im)

    def open_out(self):
        """ Open the source and reference images for reading, and output image(s) for writing. """
        self._out_im = rio.open(self._homo_filename, 'w', **self._create_out_profile())
        self._set_homo_metadata(self._out_im)

        # TODO: since param_image is the only (?) item in _config that is used in RasterFuse, rather just have a
        #  self.param_image attr
        if self._config['param_image']:
            self._param_im = rio.open(self._param_filename, 'w', **self._create_param_profile())
            self._set_param_metadata(self._param_im)

    def close_out(self):
        """ Close all open images. """
        self._out_im.close()
        if self._param_im:
            self._param_im.close()

    @contextmanager
    def out_manager(self):
        self.open_out()
        yield
        self.close_out()

    def _build_overviews(self, im, max_num_levels=8, min_level_pixels=256):
        """
        Build internal overviews, downsampled by successive powers of 2, for a rasterio dataset.

        Parameters
        ----------
        im: rasterio.io.DatasetWriter
            An open rasterio dataset to write the metadata to.
        max_num_levels: int, optional
            Maximum number of overview levels to build.
        min_level_pixels: int, pixel
            Minimum width/height (in pixels) of any overview level.
        """

        # limit overviews so that the highest level has at least 2**8=256 pixels along the shortest dimension,
        # and so there are no more than 8 levels.
        max_ovw_levels = int(np.min(np.log2(im.shape)))
        min_level_shape_pow2 = int(np.log2(min_level_pixels))
        num_ovw_levels = np.min([max_num_levels, max_ovw_levels - min_level_shape_pow2])
        ovw_levels = [2 ** m for m in range(1, num_ovw_levels + 1)]
        im.build_overviews(ovw_levels, Resampling.average)

    def build_overviews(self, max_num_levels=8, min_level_pixels=256):
        """
        Build internal overviews, downsampled by successive powers of 2, for the corrected and parameter image files.
        Should be called after homogenise().

        max_num_levels: int, optional
            Maximum number of overview levels to build.
        min_level_pixels: int, pixel
            Minimum width/height (in pixels) of any overview level.
        """

        self._assert_open()
        out_im = rio.open(self._homo_filename, 'r+')
        with out_im:
            self._build_overviews(out_im, max_num_levels=max_num_levels, min_level_pixels=min_level_pixels)
            if self._config['param_image']:
                param_im = rio.open(self._param_filename, 'r+')
                with param_im:
                    self._build_overviews(param_im, max_num_levels=max_num_levels, min_level_pixels=min_level_pixels)

    def _process_block(self, block_pair: BlockPair, model: KernelModel):
        """ Thread-safe function to homogenise a source image block. """

        # read source and reference blocks
        src_ra, ref_ra = self.read(block_pair)
        # fit and apply the sliding kernel models
        param_ra = model.fit(ref_ra, src_ra)
        out_ra = model.apply(src_ra, param_ra)
        # change the output nodata value so that is is masked correctly for out_im
        out_ra.nodata = self._out_im.nodata

        with self._write_lock:  # write the output block
            out_ra.to_rio_dataset(self._out_im, indexes=block_pair.band_i + 1, window=block_pair.src_out_block)

        if self._config['param_image']:
            with self._param_lock:  # write the parameter block
                indexes = np.arange(param_ra.count) * len(self.src_bands) + block_pair.band_i + 1
                param_out_block = (
                    block_pair.ref_out_block if self.proc_crs == ProcCrs.ref else block_pair.src_out_block
                )
                param_ra.to_rio_dataset(self._param_im, indexes=indexes, window=param_out_block)

    def process(
        self, out_filename: Union[Path, str], method: Method, kernel_shape: Tuple[int, int], overwrite: bool = False,
        model_config: Dict = None, out_profile: Dict = None, fuse_config: Dict = None, build_ovw: bool = True,
    ):
        """
        Correct the source image in blocks.

        Parameters
        ----------
        out_filename: str, Path
            Path to the corrected file to create, or a directory in which in which to create an automatically named
            file.
        method: homonim.enums.Method
            The surface reflectance correction method.
        kernel_shape: tuple
            The (height, width) of the kernel in pixels of the proc_crs image (the lowest resolution image, if
            proc_crs=ProcCrs.auto).
        overwrite: bool, optional
            Overwrite the output file(s) if they exist. [default: True]
        fuse_config: dict, optional
            Surface reflectance correction configuration with items:
                param_image: bool
                    Turn on/off the production of a debug image containing correction parameters and R2 values.
                threads: int
                    The number of blocks process concurrently (requires more memory). 0 = use all cpus.
                max_block_mem: float
                    An upper limit on the image block size in MB.  Useful for limiting the memory used by each block-
                    processing thread. Note that this is not a limit on the total memory consumed by a thread, as a
                    number of working block-sized arrays are created, but is proportional to the total memory consumed
                    by a thread.
        model_config: dict, optional
            Radiometric modelling configuration dict with items:
                downsampling: rasterio.enums.Resampling, str
                    Resampling method for downsampling.
                upsampling: rasterio.enums.Resampling, str
                    Resampling method for upsampling.
                r2_inpaint_thresh: float
                    R2 threshold below which to inpaint model parameters from "surrounding areas.
                    For 'gain-offset' method only.
                mask_partial: bool
                    Mask corrected pixels produced from partial kernel or image coverage.
        out_profile: dict, optional
            Output image configuration dict with the items:
                driver: str
                    Output format driver.
                dtype: str
                    Output image data type.
                nodata: float
                    Output image nodata value.
                creation_options: dict
                    Driver specific creation options.  See the rasterio documentation for more information.
        build_ovw: bool
            Whether to build overviews.
        """
        # call out_filename something else, in unit tests too, fuse_filename, corr_filename...?
        # TODO: might we pass in a model, rather than its config
        # TODO: pass in param_image, threads, max_block_mem here too i.e. fuse_config?
        # TODO: can we make at least some of these kwargs to simplify passing from cli and to self._process_block
        self._assert_open()
        self._method = Method(method)
        self._kernel_shape = utils.validate_kernel_shape(kernel_shape, method=method)
        overlap = utils.overlap_for_kernel(self._kernel_shape)
        self._out_profile = self.create_out_profile(**(out_profile or {}))
        self._config = fuse_config if fuse_config else self.default_fuse_config
        self._config['threads'] = utils.validate_threads(self._config['threads'])

        # create the KernelModel according to proc_crs
        model_cls = SrcSpaceModel if self.proc_crs == ProcCrs.src else RefSpaceModel
        self._model = model_cls(
            method=method, kernel_shape=kernel_shape, find_r2=self._config['param_image'], **(model_config or {}),
        )
        self._init_out_filenames(Path(out_filename), overwrite)

        block_pair_args = dict(overlap=overlap, max_block_mem=self._config['max_block_mem'])
        # get block pairs and process
        bar_format = '{l_bar}{bar}|{n_fmt}/{total_fmt} blocks [{elapsed}<{remaining}]'  # tqdm progress bar format
        with self.out_manager():
            if self._config['threads'] == 1:
                # process blocks consecutively in the main thread (useful for profiling)
                block_pairs = [block_pair for block_pair in self.block_pairs(**block_pair_args)]
                for block_pair in tqdm(block_pairs, bar_format=bar_format):
                    self._process_block(block_pair, self._model)
            else:
                # process blocks concurrently
                with futures.ThreadPoolExecutor(max_workers=self._config['threads']) as executor:
                    # submit blocks for processing
                    proc_futures = [
                        executor.submit(self._process_block, block_pair, self._model)
                        for block_pair in self.block_pairs(**block_pair_args)
                    ]

                    # wait for threads in order of completion, and raise any thread generated exceptions
                    for future in tqdm(
                        futures.as_completed(proc_futures), bar_format=bar_format, total=len(proc_futures)
                    ): # yapf: disable
                        future.result()

        if build_ovw:
            # TODO: tidy and perhaps do this while files are open, rather than open them again
            self.build_overviews()