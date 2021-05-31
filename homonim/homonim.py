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
                    # find source image bounds in DEM CRS
                    ref_roi_bounds = transform_bounds(src_im.crs, ref_im.crs, *src_im.bounds)
                    ref_win = rio.windows.from_bounds(*ref_roi_bounds, transform=ref_im.transform)

                    # read DEM in source image ROI and find minimum
                    # TODO: consider reading in tiles for large ref ims
                    # TODO: work in a particular dtype, or transform to a particular dtype here/somewhere?
                    # TODO: how to deal with pixel alignment
                    _ref_array = ref_im.read(range(1, src_im.count+1), window=ref_win)

                    if src_im.crs != ref_im.crs:    # re-project the reference image to source CRS
                        logger.info('Reprojecting reference image to the source CRS. \n'
                                    'To avoid this step, provide a reference image in the source CRS')
                        ref_transform, width, height = calculate_default_transform(ref_im.crs, src_im.crs, ref_win.width,
                                                                               ref_win.height, *ref_roi_bounds)
                        ref_array = np.zeros((height, width, src_im.count), dtype=np.float32)
                        reproject(_ref_array, self.ref_array, src_transform=ref_im.transform, src_crs=ref_im.crs,
                            dst_transform=ref_transform, dst_crs=src_im.crs, resampling=Resampling.bilinear,
                                  num_threads='all_cpus')
                    else:
                        ref_array = _ref_array.astype('float32')
                        ref_transform = rio.windows.transform(ref_win, ref_im.transform)
                    ref_profile = ref_im.profile
                    ref_profile['transform'] = ref_transform
                    ref_profile['res'] = list(ref_im.res)    # TODO: find a better way of passing this

        return ref_array, ref_profile

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
                # read and process the source image in blocks to save memory
                proc_win = dict(width=src_im.width, height=256)
                for col_off in range(0, src_im.width, proc_win['width']):
                    for row_off in range(0, src_im.height, proc_win['height']):
                        # TODO: we need to make sure we don't read a src_win of height (or width) that will result in less than self.win_size in ref space
                        # rather make the last window larger not smaller i.e. incorporate the second last full win and the remainder
                        # TODO: consider reading/writing by band for mem efficiency
                        src_win = rio.windows.Window(col_off, row_off, np.min((proc_win['width'], src_im.width - col_off)),
                                                     np.min((proc_win['height'], src_im.height - row_off)))
                        bands = list(range(1, src_im.count + 1))
                        src_win_array = src_im.read(bands, window=src_win).astype(np.float32)  # NB bands along first dim
                        src_win_transform = rio.windows.transform(src_win, src_im.transform)
                        src_win_bounds  = rio.windows.bounds(src_win, src_win_transform)

                        # calculate a transform for src_win that will align it with the ref grid
                        src_win_ds_transform, win_ds_width, win_ds_height = \
                            calculate_default_transform(src_im.crs, src_im.crs, src_win.width, src_win.height,
                                                        *src_win_bounds, resolution=ref_profile['res'])

                        # downsample the source window into the reference CRS and gid
                        src_win_ds_array = np.zeros((src_im.count, win_ds_height, win_ds_width), dtype=np.float32)
                        _, xform = reproject(src_win_array, destination=src_win_ds_array,
                                             src_transform=src_win_transform, src_crs=src_im.crs,
                                             dst_transform=src_win_ds_transform, dst_crs=src_im.crs,
                                             resampling=Resampling.average, num_threads=multiprocessing.cpu_count())

##

