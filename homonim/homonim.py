"""
    Homonim: Radiometric homogenisation of aerial and satellite imagery
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
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
import rasterio as rio
# from rasterio import transform
from rasterio.warp import reproject, Resampling, transform_geom, transform_bounds, calculate_default_transform
from enum import Enum
import pathlib
from shapely.geometry import box
from homonim import get_logger
import multiprocessing

logger = get_logger(__name__)

class Model(Enum):
    GAIN_ONLY = 1
    OFFSET_ONLY = 2
    GAIN_AND_OFFSET = 3
    GAIN_AND_IMAGE_OFFSET = 4
    OFFSET_AND_IMAGE_GAIN = 5
    GAIN_ZERO_MEAN_OFFSET = 6

def expand_window_to_grid(win):
    """
    Expands float window extents to be integers that include the original extents

    Parameters
    ----------
    win : rasterio.windows.Window
        the window to expand

    Returns
    -------
    exp_win: rasterio.windows.Window
        the expanded window
    """
    col_off, col_frac = np.divmod(win.col_off, 1)
    row_off, row_frac = np.divmod(win.row_off, 1)
    width = np.ceil(win.width + col_frac)
    height = np.ceil(win.height + row_frac)
    exp_win = rio.windows.Window(col_off, row_off, width, height)
    return exp_win


class HomonIm:
    def __init__(self, src_filename, ref_filename, win_size=[3, 3], model=Model.GAIN_ONLY):
        """
        Class for homogenising images

        Parameters
        ----------
        src_filename : str
            Source image filename
        ref_filename: str
            Reference image filename
        win_size: numpy.array_like
            (optional) Window size [width, height] in reference image pixels
        model : Model
            (optional) Model type
        """
        self._src_filename = pathlib.Path(src_filename)
        self._ref_filename = pathlib.Path(ref_filename)
        self._check_rasters()
        if not np.all(np.mod(win_size, 2) == 1):
            raise Exception('win_size must be odd in both dimensions')
        self.win_size = win_size

    def _check_rasters(self):
        """
        Check bounds, band count, and compression type of source and reference images
        """
        if not self._src_filename.exists():
            raise Exception(f'Source file {self._src_filename.stem} does not exist' )
        if not self._ref_filename.exists():
            raise Exception(f'Reference file {self._ref_filename.stem} does not exist' )

        with rio.Env(GDAL_NUM_THREADS='ALL_CPUs'):
            # check we can read the images
            for filename in [self._src_filename, self._ref_filename]:
                with rio.open(filename, 'r') as im:
                    try:
                        tmp_array = im.read(1, window=im.block_window(1, 0, 0))
                    except Exception as ex:
                        if im.profile['compress'] == 'jpeg':  # assume it is a 12bit JPEG
                            raise Exception(f'Could not read {filename.stem}\n'
                                            f'    This GDAL package does not support JPEG compression with NBITS==12\n'
                                            f'    you probably need to recompress this file.\n'
                                            f'    See the README for details.')
                        else:
                            raise ex

            with rio.open(self._src_filename, 'r') as src_im:
                with rio.open(self._ref_filename, 'r') as ref_im:
                    # check reference covers source
                    src_box = box(*src_im.bounds)
                    ref_box = box(*ref_im.bounds)

                    # reproject the reference bounds if necessary
                    if src_im.crs != ref_im.crs:    # CRS's don't match
                        ref_box = transform_geom(ref_im.crs, src_im.crs, ref_box)

                    if not ref_box.covers(src_box):
                        raise Exception(f'Reference image {self._ref_filename.stem} does not cover source image '
                                        f'{self._src_filename.stem}')

                    # check reference has enough bands for the source
                    if src_im.count > ref_im.count:
                        raise Exception(f'Reference image {self._ref_filename.stem} has fewer bands than source image '
                                        f'{self._src_filename.stem}')

                    # if the band counts don't match assume the first src_im.count bands of ref_im match those of src_im
                    if src_im.count != ref_im.count:
                        logger.warning('Reference bands do not match source bands.  \n'
                                       'Using the first {src_im.count} bands of reference image  {self._ref_filename.stem}')

                    # if src_im.crs != ref_im.crs:    # CRS's don't match
                    #     logger.warning('The reference will be re-projected to the source CRS.  \n'
                    #                    'To avoid this step, provide a reference image in the source CRS')

    def _read_ref(self):
        """
        Read the source region from the reference image in the source CRS
        """
        with rio.Env(GDAL_NUM_THREADS='ALL_CPUs'):
            with rio.open(self._src_filename, 'r') as src_im:
                with rio.open(self._ref_filename, 'r') as ref_im:
                    # find source source bounds in ref CRS  (possibly same CRS)
                    ref_src_bounds = transform_bounds(src_im.crs, ref_im.crs, *src_im.bounds)

                    # find ref window that covers source and is aligned with ref grid
                    ref_src_win = rio.windows.from_bounds(*ref_src_bounds, transform=ref_im.transform)
                    ref_src_win = expand_window_to_grid(ref_src_win)

                    # transform and bounds of ref_src_win in ref CRS
                    ref_src_transform = rio.windows.transform(ref_src_win, ref_im.transform)
                    ref_src_bounds = rio.windows.bounds(ref_src_win, ref_im.transform)

                    # TODO: consider reading in tiles for large ref ims
                    # TODO: work in a particular dtype, or transform to a particular dtype here/somewhere?
                    # TODO: how to deal with pixel alignment i.e. if ref_win is float below,
                    #   including resampling below with a non-NN option, does resample to the float window offsets
                    #   but should we not rather resample the source to the ref grid than sort of the other way around

                    ref_bands = range(1, src_im.count + 1)
                    _ref_array = ref_im.read(ref_bands, window=ref_src_win).astype('float32')

                    if src_im.crs != ref_im.crs:    # re-project the reference image to source CRS
                        # TODO: here we reproject from transform on the ref grid and that include source bounds
                        #   we could just project into src_im transform though too.
                        logger.info('Reprojecting reference image to the source CRS. \n'
                                    '\tTo avoid this step, provide a reference and source images should be provided in the same CRS')

                        # transform and dimensions of reprojected ref ROI
                        ref_transform, width, height = calculate_default_transform(ref_im.crs, src_im.crs, ref_src_win.width,
                                                                               ref_src_win.height, *ref_src_bounds)
                        ref_array = np.zeros((src_im.count, height, width), dtype=np.float32)
                        # TODO: another thing to think of is that we ideally shouldn't reproject the reference unnecessarily to avoid damaging radiometric info
                        #  this could mean we reproject the source to/from ref CRS rather than just resample it
                        _, _ = reproject(_ref_array, ref_array, src_transform=ref_src_transform, src_crs=ref_im.crs,
                            dst_transform=ref_transform, dst_crs=src_im.crs, resampling=Resampling.bilinear,
                                  num_threads=multiprocessing.cpu_count())
                    else:
                        ref_transform = ref_src_transform
                        ref_array = _ref_array

                    ref_profile = ref_im.profile
                    ref_profile['transform'] = ref_transform
                    ref_profile['res'] = list(ref_im.res)    # TODO: find a better way of passing this
                    ref_profile['bounds'] = ref_src_bounds

        return ref_array, ref_profile

    def _sliding_window_view(self, x):
        """
        Return a 3D strided view of 2D array to allow fast sliding window operations.
        Rolling windows are stacked along the third dimension.  No data copying is involved.

        Parameters
        ----------
        x : numpy.array_like
            array to return view of

        Returns
        -------
        3D rolling window view of x
        """
        xstep = 1
        shape = x.shape[:-1] + (self.win_size[0], int(1 + (x.shape[-1] - self.win_size[0]) / xstep))
        strides = x.strides[:-1] + (x.strides[-1], xstep * x.strides[-1])
        return np.lib.stride_tricks.as_strided(x, shape=shape, strides=strides, writeable=False)

    def _find_band_params_gain_only(self, ref_array, src_array):
        """
        Find the sliding window calibration parameters for a band

        Parameters
        ----------
        ref_array : numpy.array_like
            a reference band in an MxN array
        src_array : numpy.array_like
            a source band, collocated with ref_array and of the same MxN shape

        Returns
        -------
        param_array : numpy.array_like
            an M x N x P array of calibration parameters, where P is the number of parameters that varies according to
            the selected Model
        """
        ratio_array = np.zeros_like(src_array, dtype=np.float32)
        _ = np.divide(ref_array, src_array, out=ratio_array, where=(src_array!=0))   # find ratios once
        ratio_winview = sliding_window_view(ratio_array, self.win_size)  # apply the sliding window to the ratios

        param_array = np.zeros_like(ref_array, dtype=np.float32)
        win_offset = np.floor(np.array(self.win_size)/2).astype(np.int32)      # the window center
        _ = np.mean(ratio_winview, out=param_array[win_offset[1]:-win_offset[1], win_offset[0]:-win_offset[0]],
                    axis=(2, 3))        # find mean ratio over sliding windows

        # TODO: what is the most efficient way to iterate over these view arrays? np.nditer?
        #   or might we use a cython inner to speed things up?  See np.nditer docs
        #   is the above mean over sliding win views faster than the nested loop below
        # for win_i in np.ndindex(ratio_winview.shape[2]):
        #     for win_j in np.ndindex(ratio_winview.shape[3]):
        #         ratio_win = ratio_winview[:, :, win_i, win_j]
        #         param_array[win_i + win_offset[1], win_j + win_offset[0]] = np.mean(ratio_win)  # gain only

        return param_array


    def homogenise(self, out_filename):
        """
        Perform homogenisation

        Parameters
        ----------
        out_filename : str
            name of the file to save the homogenised image to
        """
        ref_array, ref_profile = self._read_ref()
        with rio.Env(GDAL_NUM_THREADS='ALL_CPUs'):
            with rio.open(self._src_filename, 'r') as src_im:

                calib_profile = src_im.profile
                calib_profile.update(num_threads=multiprocessing.cpu_count(), compress='deflate', interleave='band',
                                     nodata=0)
                with rio.open(out_filename, 'w', **calib_profile) as calib_im:

                    # process by band to limit memory usage
                    bands = list(range(1, src_im.count + 1))
                    for bi in bands:
                        src_array = src_im.read(bi)  # NB bands along first dim

                        # downsample the source window into the reference CRS and gid
                        src_ds_array = np.zeros((ref_array.shape[1], ref_array.shape[2]), dtype=np.float32)
                        # TODO: resample rather than reproject?
                        # TODO: also think if there is a neater way we can do this, rather than having arrays and transforms in mem
                        #   we have datasets / memory images ?

                        _, xform = reproject(src_array, destination=src_ds_array,
                                             src_transform=src_im.transform, src_crs=src_im.crs,
                                             dst_transform=ref_profile['transform'], dst_crs=ref_profile['crs'],
                                             resampling=Resampling.average, num_threads=multiprocessing.cpu_count(),
                                             src_nodata=0, dst_nodata=0)

                        # find the calibration parameters for this band
                        param_ds_array = self._find_band_params_gain_only(ref_array[bi-1, :, :], src_ds_array)

                        # upsample the parameter array
                        param_array = np.zeros_like(src_array, dtype=np.float32)
                        _, xform = reproject(param_ds_array, destination=param_array,
                                             src_transform=ref_profile['transform'], src_crs=ref_profile['crs'],
                                             dst_transform=src_im.transform, dst_crs=src_im.crs,
                                             resampling=Resampling.cubic_spline, num_threads=multiprocessing.cpu_count(),
                                             src_nodata=0, dst_nodata=0)

                        # apply the calibration and write
                        calib_src_array = param_array * src_array
                        calib_im.write(calib_src_array.astype(calib_im.dtypes[bi-1]), indexes=bi)

##

