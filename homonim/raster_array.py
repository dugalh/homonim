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

import multiprocessing
import logging

import numpy as np
import rasterio.windows
from rasterio import Affine
from rasterio import transform
from rasterio import windows
from rasterio.crs import CRS
from rasterio.enums import MaskFlags, ColorInterp
from rasterio.warp import reproject, Resampling
from rasterio.windows import Window

from homonim.errors import ImageProfileError, ImageFormatError, RasterArrayFormatError

logger = logging.getLogger(__name__)


def nan_equals(a, b, equal_nan=True):
    if not equal_nan:
        return (a == b)
    else:
        return ((a == b) | (np.isnan(a) & np.isnan(b)))


def expand_window_to_grid(win, expand_pixels=(0, 0)):
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
    col_off, col_frac = np.divmod(win.col_off - expand_pixels[1], 1)
    row_off, row_frac = np.divmod(win.row_off - expand_pixels[0], 1)
    width = np.ceil(win.width + 2 * expand_pixels[1] + col_frac)
    height = np.ceil(win.height + 2 * expand_pixels[0] + row_frac)
    exp_win = Window(col_off.astype('int'), row_off.astype('int'), width.astype('int'), height.astype('int'))
    return exp_win

# TODO: think about how the round to even issue impacts on this
def round_window_to_grid(win):
    """
    Rounds float window extents to nearest integer

    Parameters
    ----------
    win : rasterio.windows.Window
        the window to round

    Returns
    -------
    exp_win: rasterio.windows.Window
        the rounded window
    """
    row_range, col_range = win.toranges()
    row_range = np.round(row_range).astype('int')
    col_range = np.round(col_range).astype('int')
    return Window(col_off=col_range[0], row_off=row_range[0], width=np.diff(col_range)[0], height=np.diff(row_range)[0])


class RasterArray(transform.TransformMethodsMixin, windows.WindowMethodsMixin):
    """
    A class for wrapping and re-projecting a geo-referenced numpy array.
    Internally masking is done using a nodata value, not a separately stored mask.
    By default internal data type is float32 and the nodata value is nan.
    """
    default_nodata = float('nan')
    default_dtype = 'float32'

    def __init__(self, array, crs, transform, nodata=default_nodata, window=None):
        # array = np.array(array)
        if (array.ndim < 2) or (array.ndim > 3):
            raise ValueError('"array" must be have 2 or 3 dimensions with bands along the first dimension')
        self._array = array

        if window is not None:
            if (window.height, window.width) != array.shape[-2:]:
                raise ValueError('"window" and "array" width and height must match')

        if isinstance(crs, CRS):
            self._crs = crs
        else:
            raise TypeError('"crs" must be an instance of rasterio.CRS')

        if isinstance(transform, Affine):
            if window is not None:
                self._transform = windows.transform(window, transform)
            else:
                self._transform = transform
        else:
            raise TypeError('"transform" must be an instance of rasterio.Affine')

        self._nodata = nodata
        self._nodata_mask = None

    @staticmethod
    def bounded_window_slices(window: rasterio.windows.Window, rio_dataset: rasterio.DatasetReader):
        """ Bounded array slices and dataset window from boundless window and dataset """
        win_ul = np.array((window.row_off, window.col_off))
        win_br = win_ul + np.array((window.height, window.width))
        bounded_ul = np.fmax(win_ul, (0, 0))
        bounded_br = np.fmin(win_br, rio_dataset.shape)
        bounded_window = Window.from_slices((bounded_ul[0], bounded_br[0]), (bounded_ul[1], bounded_br[1]))
        bounded_start = bounded_ul - win_ul
        bounded_stop = bounded_start + (bounded_br - bounded_ul)
        bounded_slices = (slice(bounded_start[0], bounded_stop[0], None),
                          slice(bounded_start[1], bounded_stop[1], None))
        return bounded_window, bounded_slices

    @classmethod
    def from_profile(cls, array, profile, window=None):
        if not ('crs' and 'transform' and 'nodata' in profile):
            raise ImageProfileError('"profile" should include "crs", "transform" and "nodata" keys')
        if array is None:  # create array filled with nodata
            if not ('width' and 'height' and 'count' and 'dtype' in profile):
                raise ImageProfileError('"profile" should include "width", "height", "count" and "dtype" keys')
            array_shape = (profile['count'], profile['height'], profile['width'])
            array = np.full(array_shape, fill_value=profile['nodata'], dtype=profile['dtype'])
        return cls(array, profile['crs'], profile['transform'], nodata=profile['nodata'], window=window)

    @classmethod
    def from_rio_dataset(cls, rio_dataset, indexes=None, window=None, **kwargs):
        if indexes is None:
            index_list = [bi + 1 for bi in range(rio_dataset.count) if rio_dataset.colorinterp[bi] != ColorInterp.alpha]
        else:
            index_list = [indexes] if np.isscalar(indexes) else indexes
        # check bands if bands have masks (i.e. internal/side-car mask or alpha channel, as opposed to nodata value)
        is_masked = any([MaskFlags.per_dataset in rio_dataset.mask_flag_enums[bi - 1] for bi in index_list])

        # homonim implementation of boundless=True and fill_value=x, as rasterio's is slow
        nodata = cls.default_nodata if (is_masked or rio_dataset.nodata is None) else rio_dataset.nodata
        array = np.full((window.height, window.width), fill_value=nodata, dtype=cls.default_dtype)
        if window:
            bounded_window, bounded_slices = cls.bounded_window_slices(window, rio_dataset)
            bounded_array = array[bounded_slices]   # bounded view into array
        else:
            bounded_window = window
            bounded_array = array

        rio_dataset.read(out=bounded_array, indexes=indexes, window=bounded_window, out_dtype=cls.default_dtype,
                         **kwargs)

        if is_masked:
            # read mask from dataset and apply it to array
            bounded_mask = rio_dataset.dataset_mask(window=bounded_window).astype('bool', copy=False)
            bounded_array[~bounded_mask] = nodata

        return cls(array, rio_dataset.crs, rio_dataset.transform, nodata=nodata, window=window)

    @property
    def array(self):
        return self._array

    @array.setter
    def array(self, value):
        if np.all(value.shape[-2:] == self._array.shape[-2:]):
            self._array = value
        else:
            raise ValueError('"value" and current width and height must match')

    @property
    def crs(self):
        return self._crs

    @property
    def width(self):
        return self.shape[-1]

    @property
    def height(self):
        return self.shape[-2]

    @property
    def shape(self):
        return self._array.shape[-2:]

    @property
    def count(self):
        return self._array.shape[0] if self.array.ndim == 3 else 1

    @property
    def dtype(self):
        return self._array.dtype

    @property
    def transform(self):
        return self._transform

    @property
    def res(self):
        return np.abs((self._transform.a, self._transform.e))

    @property
    def bounds(self):
        return windows.bounds(windows.Window(0, 0, self.width, self.height), self._transform)

    @property
    def profile(self):
        return dict(crs=self._crs, transform=self._transform, nodata=self._nodata, count=self.count,
                    width=self.width, height=self.height, bounds=self.bounds, dtype=self.dtype)

    @property
    def proj_profile(self):
        return dict(crs=self._crs, transform=self._transform, shape=self.shape)

    @property
    def mask(self):
        """ 2D boolean mask corresponding to valid pixels in array """
        if self._nodata is None:
            return np.full(self.shape, True)
        mask = ~nan_equals(self.array, self.nodata)
        if self._array.ndim > 2:
            mask = np.all(mask, axis=0)
        return mask

    @mask.setter
    def mask(self, value):
        """ 2D boolean mask corresponding to valid pixels in array """
        if self._array.ndim == 2:
            self._array[~value] = self._nodata
        else:
            self._array[:, ~value] = self._nodata

    @property
    def nodata(self):
        """ nodata value """
        return self._nodata

    @nodata.setter
    def nodata(self, value):
        """ nodata value """
        if value is None or self._nodata is None:
            # if value is None, remove the mask, if current nodata is None,
            # there is no mask to incorporate the new value into array
            self._nodata = value
        elif not (nan_equals(value, self._nodata)):
            # if value is different to current nodata, set mask area in array to value
            nodata_mask = ~self.mask
            if self._array.ndim == 3:
                self._array[:, nodata_mask] = value
            else:
                self._array[nodata_mask] = value
            self._nodata = value

    def copy(self, deep=True):
        array = self._array.copy() if deep else self._array
        return RasterArray.from_profile(array, self.profile)

    def slice_array(self, *bounds):
        window = self.window(*bounds)
        window = round_window_to_grid(window)   # TODO: necessary?  and error checking on window and array shape
        if self._array.ndim == 2:
            array = self._array[window.toslices()]
        else:
            array = self._array[(slice(self._array.shape[0]), *window.toslices())]
        return RasterArray(array, self._crs, self.window_transform(window), nodata=self._nodata)

    def to_rio_dataset(self, rio_dataset: rasterio.io.DatasetWriter, indexes=None, window=None, **kwargs):
        """
        Write RasterArray to a rasterio dataset

        Parameters
        ----------
        rio_dataset: rasterio.io.DatasetWriter
                     Open rasterio dataset into which to write the RasterArray.  The dataset CRS must match that of
                     the RasterArray.
        indexes: int, list[int], optional
                 1 based band index or list of band indices to write to in the dataset.
                 The number of indices must correspond to the RasterArray count (number of bands).
                 [default: write all dataset non-alpha bands.]
        window: rasterio.windows.Window, optional
                A window defining the region in the dataset to write to.  Can be a 'boundless' window i.e. extended
                beyond the bounds of the dataset, in which case it will be cropped to fit the bounds of the dataset.
                The RasterArray will then be cropped to fit the bounded window as necessary.
                [default: write to the full extent of the dataset.]
        kwargs: optional
                Arguments to passed through the dataset's write() method.
        """
        if rio_dataset.crs != self._crs:
            raise ImageFormatError(f"The dataset CRS does not match that of the RasterArray. "
                                    f"Dataset: {rio_dataset.crs.to_proj4()}, "
                                    f"RastterArray: {rio_dataset.crs.to_proj4()}")
        if indexes is None:
            indexes = [bi + 1 for bi in range(rio_dataset.count) if rio_dataset.colorinterp[bi] != ColorInterp.alpha]

        if np.any(np.array(indexes) > rio_dataset.count):
            error_indexes = np.array(indexes)[np.array(indexes) > rio_dataset.count]
            raise ValueError(f'Band index(es) ({error_indexes}) exceed the dataset count ({rio_dataset.count})')

        if (isinstance(indexes, list) and (len(indexes) > self.count)):
            raise ValueError(f'The length of indexes ({len(indexes)}) exceeds the number of bands in the '
                                   f'RasterArray ({self.count})')

        if window is None:
            window = rio_dataset.window(*self.bounds)
            window, _ = self.bounded_window_slices(window, rio_dataset)
            # window = Window(col_off=0, row_off=0, width=rio_dataset.width, height=rio_dataset.height)
        else:
            # crop the window to dataset bounds (if necessary)
            _window = window
            window, _ = self.bounded_window_slices(window, rio_dataset)

        # a bounded view into the array to match the dataset window (may be full array)
        bounded_ra = self.slice_array(*rio_dataset.window_bounds(window))
        if np.any(np.array(bounded_ra.shape) <= 0):
            raise ValueError(f'The window gives a bounded array shape ({bounded_ra.shape}) with zero length '
                             f'dimension')

        # if np.any(np.array((window.height, window.width)) != np.array(bounded_array.shape[-2:])):
        #     raise ValueError(f'The bounded window shape ({(window.height, window.width)}) does not match the bounded '
        #                      f'array shape ({bounded_array.shape[-2:]})')
        if np.any(np.array((window.height, window.width)) != np.array(bounded_ra.shape)):
            logger.warning(f'The bounded window shape ({(window.height, window.width)}) does not match the bounded '
                             f'array shape ({bounded_ra.shape[-2:]})')

        rio_dataset.write(bounded_ra.array, window=window, indexes=indexes, **kwargs)


    def reproject(self, crs=None, transform=None, shape=None, nodata=default_nodata, dtype=default_dtype,
                  resampling=Resampling.lanczos, **kwargs):

        if transform and not shape:
            raise ValueError('If "transform" is specified, "shape" must also be specified')

        if isinstance(resampling, str):
            resampling = Resampling[resampling]

        crs = crs or self._crs
        shape = shape or self._array.shape
        dtype = dtype or self._array.dtype

        if self.array.ndim > 2:
            _dst_array = np.zeros((self._array.shape[0], *shape), dtype=dtype)
        else:
            _dst_array = np.zeros(shape, dtype=dtype)

        _, _dst_transform = reproject(
            self._array,
            destination=_dst_array,
            src_crs=self._crs,
            src_transform=self._transform,
            src_nodata=self._nodata,
            dst_crs=crs,
            dst_transform=transform,
            dst_nodata=nodata,
            num_threads=multiprocessing.cpu_count(),
            resampling=resampling,
            **kwargs
        )
        return RasterArray(_dst_array, crs=crs, transform=_dst_transform, nodata=nodata)

##
