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
import os

import pytest
import rasterio as rio
from rasterio.features import shapes

from homonim.enums import ProcCrs, Method
from homonim.fuse import RasterFuse
from homonim.kernel_model import KernelModel


@pytest.mark.parametrize('src_file, ref_file, method', [
    ('float_50cm_src_file', 'float_100cm_ref_file', Method.gain),
    ('float_50cm_src_file', 'float_100cm_ref_file', Method.gain_blk_offset),
    ('float_50cm_src_file', 'float_100cm_ref_file', Method.gain_offset),
    ('float_100cm_src_file', 'float_50cm_ref_file', Method.gain),
    ('float_100cm_src_file', 'float_50cm_ref_file', Method.gain_blk_offset),
    ('float_100cm_src_file', 'float_50cm_ref_file', Method.gain_offset),
])
def test_creation(src_file, ref_file, method, tmp_path, request):
    """Test creation of RasterFuse"""
    src_file = request.getfixturevalue(src_file)
    ref_file = request.getfixturevalue(ref_file)
    model_config = KernelModel.default_config.copy()
    model_config.update(mask_partial=True)
    homo_config = RasterFuse.default_homo_config.copy()
    homo_config.update(param_image=True)
    out_profile = RasterFuse.default_out_profile.copy()
    out_profile.update(driver='HFA', creation_options={})

    raster_fuse = RasterFuse(src_file, ref_file, tmp_path, method, (5, 5), homo_config=homo_config,
                             model_config=model_config, out_profile=out_profile)
    with raster_fuse:
        assert (raster_fuse.method == method)
        assert (raster_fuse.kernel_shape == (5, 5))
        assert (raster_fuse.proc_crs != ProcCrs.auto)
        assert (raster_fuse.homo_filename is not None)
        assert (raster_fuse.param_filename is not None)
        assert (not raster_fuse.closed)

        assert (raster_fuse._config == homo_config)
        assert (raster_fuse._model_config == model_config)
        for k, v in model_config.items():
            assert (raster_fuse._model.__getattribute__(f'_{k}') == v)
        assert (raster_fuse._out_profile == out_profile)

    assert (raster_fuse.closed)


@pytest.mark.parametrize('overwrite', [False, True])
def test_overwrite(tmp_path, float_50cm_src_file, float_100cm_ref_file, overwrite):
    homo_config = RasterFuse.default_homo_config.copy()
    homo_config.update(param_image=True)
    params = dict(src_filename=float_50cm_src_file, ref_filename=float_100cm_ref_file, homo_path=tmp_path,
                  method=Method.gain_blk_offset, kernel_shape=(5, 5), homo_config=homo_config, overwrite=overwrite)

    raster_fuse = RasterFuse(**params)
    raster_fuse.homo_filename.touch()
    if not overwrite:
        with pytest.raises(FileExistsError):
            _ = RasterFuse(**params)
    else:
        _ = RasterFuse(**params)

    os.remove(raster_fuse.homo_filename)
    raster_fuse.param_filename.touch()
    if not overwrite:
        with pytest.raises(FileExistsError):
            _ = RasterFuse(**params)
    else:
        _ = RasterFuse(**params)


@pytest.mark.parametrize('src_file, ref_file, method, kernel_shape, max_block_mem', [
    ('float_45cm_src_file', 'float_100cm_ref_file', Method.gain, (1, 1), 2.e-4),
    ('float_45cm_src_file', 'float_100cm_ref_file', Method.gain_blk_offset, (1, 1), 1.e-3),
    ('float_45cm_src_file', 'float_100cm_ref_file', Method.gain_offset, (5, 5), 1.e-3),
    ('float_100cm_src_file', 'float_45cm_ref_file', Method.gain, (1, 1), 2.e-4),
    ('float_100cm_src_file', 'float_45cm_ref_file', Method.gain_blk_offset, (1, 1), 1.e-3),
    ('float_100cm_src_file', 'float_45cm_ref_file', Method.gain_offset, (5, 5), 1.e-3),
])
def test_basic_fusion(src_file, ref_file, method, kernel_shape, max_block_mem, tmp_path, request):
    """"""
    src_file = request.getfixturevalue(src_file)
    ref_file = request.getfixturevalue(ref_file)
    homo_config = RasterFuse.default_homo_config.copy()
    homo_config.update(max_block_mem=max_block_mem)
    raster_fuse = RasterFuse(src_file, ref_file, tmp_path, method, kernel_shape, homo_config=homo_config)
    with raster_fuse:
        raster_fuse.process()
    assert (raster_fuse.homo_filename.exists())
    with rio.open(src_file, 'r') as src_ds, rio.open(raster_fuse.homo_filename, 'r') as out_ds:
        src_array = src_ds.read(indexes=1)
        src_mask = src_ds.dataset_mask().astype('bool', copy=False)
        out_array = out_ds.read(indexes=1)
        out_mask = out_ds.dataset_mask().astype('bool', copy=False)
        assert (out_mask == src_mask).all()
        assert (out_array[out_mask] == pytest.approx(src_array[src_mask], abs=2))


@pytest.mark.parametrize('out_profile', [
    dict(driver='GTiff', dtype='float32', nodata=float('nan'),
         creation_options=dict(tiled=True, blockxsize=512, blockysize=512, compress='deflate', interleave='band',
                               photometric=None)),
    dict(driver='GTiff', dtype='uint8', nodata=0,
         creation_options=dict(tiled=True, blockxsize=64, blockysize=64, compress='jpeg', interleave='pixel',
                               photometric='ycbcr')),
    dict(driver='PNG', dtype='uint16', nodata=0,
         creation_options=dict()),
])
def test_out_profile(float_100cm_rgb_file, tmp_path, out_profile):
    """"""
    raster_fuse = RasterFuse(float_100cm_rgb_file, float_100cm_rgb_file, tmp_path, Method.gain_blk_offset, (3, 3),
                             out_profile=out_profile)
    with raster_fuse:
        raster_fuse.process()
    assert (raster_fuse.homo_filename.exists())
    out_profile.update(**out_profile['creation_options'])
    out_profile.pop('creation_options')
    with rio.open(float_100cm_rgb_file, 'r') as src_ds, rio.open(raster_fuse.homo_filename, 'r') as out_ds:
        # test output image has been set with out_profile properties
        for k, v in out_profile.items():
            assert ((v is None and k not in out_ds.profile) or (out_ds.profile[k] == v) or
                    (str(out_ds.profile[k]) == str(v)))

        if src_ds.profile['driver'].lower() == out_profile['driver'].lower():
            # source image keys including driver specific creation options, not present in out_profile
            src_keys = set(src_ds.profile.keys()).difference(out_profile.keys())
        else:
            # source image keys excluding driver specific creation options, not present in out_profile
            src_keys = {'width', 'height', 'count', 'dtype', 'crs', 'transform'}.difference(out_profile.keys())
        # test output image has been set with src image properties not in out_profile
        for k in src_keys:
            v = src_ds.profile[k]
            assert ((v is None and k not in out_ds.profile) or (out_ds.profile[k] == v) or
                    (str(out_ds.profile[k]) == str(v)))


@pytest.mark.parametrize('method, proc_crs', [
    (Method.gain, ProcCrs.ref),
    (Method.gain_blk_offset, ProcCrs.ref),
    (Method.gain_offset, ProcCrs.ref),
    (Method.gain, ProcCrs.src),
    (Method.gain_blk_offset, ProcCrs.src),
    (Method.gain_offset, ProcCrs.src),
])
def test_param_image(float_100cm_rgb_file, tmp_path, method, proc_crs):
    homo_config = RasterFuse.default_homo_config.copy()
    homo_config.update(param_image=True)
    raster_fuse = RasterFuse(float_100cm_rgb_file, float_100cm_rgb_file, tmp_path, method, (5, 5), proc_crs=proc_crs,
                             homo_config=homo_config)
    with raster_fuse:
        raster_fuse.process()

    assert (raster_fuse.param_filename.exists())

    with rio.open(float_100cm_rgb_file, 'r') as ref_src_ds, rio.open(raster_fuse.param_filename, 'r') as param_ds:
        assert (param_ds.count == ref_src_ds.count * 3)
        param_mask = param_ds.dataset_mask().astype('bool', copy=False)
        src_ref_mask = ref_src_ds.dataset_mask().astype('bool', copy=False)
        assert (param_mask == src_ref_mask).all()


@pytest.mark.parametrize('src_file, ref_file, kernel_shape, proc_crs, mask_partial', [
    ('float_45cm_src_file', 'float_100cm_ref_file', (1, 1), ProcCrs.auto, False),
    ('float_45cm_src_file', 'float_100cm_ref_file', (1, 1), ProcCrs.auto, True),
    ('float_45cm_src_file', 'float_100cm_ref_file', (3, 3), ProcCrs.auto, True),
    ('float_100cm_src_file', 'float_45cm_ref_file', (1, 1), ProcCrs.auto, False),
    ('float_100cm_src_file', 'float_45cm_ref_file', (1, 1), ProcCrs.auto, True),
    ('float_100cm_src_file', 'float_45cm_ref_file', (3, 3), ProcCrs.auto, True),
])
def test_mask_partial(src_file, ref_file, tmp_path, kernel_shape, proc_crs, mask_partial, request):
    src_file = request.getfixturevalue(src_file)
    ref_file = request.getfixturevalue(ref_file)
    model_config = RasterFuse.default_model_config.copy()
    model_config.update(mask_partial=mask_partial)
    homo_config = RasterFuse.default_homo_config.copy()
    homo_config.update(max_block_mem=1.e-1)
    raster_fuse = RasterFuse(src_file, ref_file, tmp_path, Method.gain_blk_offset, kernel_shape,
                             proc_crs=proc_crs, model_config=model_config, homo_config=homo_config)
    with raster_fuse:
        raster_fuse.process()

    assert (raster_fuse.homo_filename.exists())
    with rio.open(src_file, 'r') as src_ds, rio.open(raster_fuse.homo_filename, 'r') as out_ds:
        out_mask = out_ds.dataset_mask().astype('bool', copy=False)
        src_mask = src_ds.dataset_mask().astype('bool', copy=False)
        if not mask_partial:
            assert (out_mask == src_mask).all()
        else:
            assert (out_mask.sum() < src_mask.sum())
            assert (out_mask.sum() > 0)
            assert (src_mask[out_mask]).all()
            out_mask_shapes = [shape for shape in shapes(out_mask.astype('uint8', copy=False), mask=out_mask)]
            assert (len(out_mask_shapes) == 1)


def test_build_overviews(float_50cm_ref_file, tmp_path):
    homo_config = RasterFuse.default_homo_config.copy()
    homo_config.update(param_image=True)
    raster_fuse = RasterFuse(float_50cm_ref_file, float_50cm_ref_file, tmp_path, Method.gain_blk_offset, (3, 3),
                             homo_config=homo_config)
    with raster_fuse:
        raster_fuse.process()
        raster_fuse.build_overviews(min_level_pixels=4)

    assert (raster_fuse.homo_filename.exists())
    assert (raster_fuse.param_filename.exists())

    with rio.open(raster_fuse.homo_filename, 'r') as out_ds:
        assert (len(out_ds.overviews(1)) > 0)

    with rio.open(raster_fuse.param_filename, 'r') as param_ds:
        for band_i in param_ds.indexes:
            assert (len(param_ds.overviews(band_i)) > 0)
