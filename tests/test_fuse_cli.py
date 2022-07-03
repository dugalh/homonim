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

import os

import pytest
import rasterio as rio
import yaml

from homonim import utils
from homonim.cli import cli
from homonim.enums import ProcCrs, Method
from homonim.fuse import RasterFuse
from tests.conftest import str_contain_nos


@pytest.mark.parametrize(
    'method, kernel_shape', [
        (Method.gain, (1, 1)),
        (Method.gain_blk_offset, (1, 1)),
        (Method.gain_offset, (5, 5)),
    ]
) # yapf: disable
def test_fuse(tmp_path, runner, float_100cm_rgb_file, float_50cm_rgb_file, method, kernel_shape):
    """ Test fuse cli output with different methods and kernel shapes. """
    ref_file = float_100cm_rgb_file
    src_file = float_50cm_rgb_file
    post_fix = utils.create_out_postfix(ProcCrs.ref, method, kernel_shape, RasterFuse.create_out_profile()['driver'])
    homo_file = tmp_path.joinpath(src_file.stem + post_fix)
    cli_str = f'fuse -m {method.value} -k {kernel_shape[0]} {kernel_shape[1]} -od {tmp_path} {src_file} {ref_file}'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)
    assert (homo_file.exists())

    with rio.open(src_file, 'r') as src_ds, rio.open(homo_file, 'r') as out_ds:
        assert (out_ds.tags()['FUSE_METHOD'] == method.name)
        assert (out_ds.tags()['FUSE_KERNEL_SHAPE'] == str(kernel_shape))

        src_array = src_ds.read(indexes=src_ds.indexes)
        src_mask = src_ds.dataset_mask().astype('bool', copy=False)
        out_array = out_ds.read(indexes=out_ds.indexes)
        out_mask = out_ds.dataset_mask().astype('bool', copy=False)
        assert (out_mask == src_mask).all()
        assert (out_array[:, out_mask] == pytest.approx(src_array[:, src_mask], abs=.1))


def test_fuse_defaults(runner, default_fuse_cli_params):
    """ Test fuse cli works without method or kernel shape arguments. """
    result = runner.invoke(cli, default_fuse_cli_params.cli_str.split())
    assert (result.exit_code == 0)
    assert (default_fuse_cli_params.homo_file.exists())


def test_method_error(runner, default_fuse_cli_params):
    """ Test unknown method generates an error. """
    cli_str = default_fuse_cli_params.cli_str + ' -m unk'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code != 0)
    assert ("Invalid value for '-m' / '--method'" in result.output)


@pytest.mark.parametrize('bad_kernel_shape', [(0, 0), (2, 3), (3, 2)])
def test_kernel_shape_error(runner, default_fuse_cli_params, bad_kernel_shape):
    """ Test bad kernel shape generates an error. """
    cli_str = default_fuse_cli_params.cli_str + f' -k {bad_kernel_shape[0]} {bad_kernel_shape[1]}'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code != 0)
    assert ("Invalid value" in result.output)


def test_file_exists_error(runner, basic_fuse_cli_params):
    """ Test that attempting to overwrite an existing output file generates an error. """
    basic_fuse_cli_params.homo_file.touch()
    cli_str = basic_fuse_cli_params.cli_str
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code != 0)
    assert ('FileExistsError' in result.output)

    os.remove(basic_fuse_cli_params.homo_file)
    basic_fuse_cli_params.param_file.touch()
    cli_str = basic_fuse_cli_params.cli_str + ' --param-image'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code != 0)
    assert ('FileExistsError' in result.output)


def test_overwrite(runner, basic_fuse_cli_params):
    """ Test overwriting existing output file(s) with -o. """
    basic_fuse_cli_params.homo_file.touch()
    basic_fuse_cli_params.param_file.touch()
    cli_str = basic_fuse_cli_params.cli_str + ' --param-image -o'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)
    assert (basic_fuse_cli_params.homo_file.exists())
    assert (basic_fuse_cli_params.param_file.exists())


def test_compare(runner, float_100cm_ref_file, float_100cm_src_file):
    """ Test --compare, in flag and value configurations, against expected output. """
    ref_file = float_100cm_ref_file
    src_file = float_100cm_src_file
    # test --compare in flag (no value), and value configuration
    cli_strs = [
        f'fuse  {src_file} {ref_file} --compare', f'fuse {src_file} {ref_file} --compare {float_100cm_ref_file} -o'
    ]
    for cli_str in cli_strs:
        result = runner.invoke(cli, cli_str.split())
        assert (result.exit_code == 0)
        src_cmp_str = """float_100cm_src.tif:
    
            r2   RMSE  rRMSE   N 
    Band 1 1.00  0.00  0.00   144
    Mean   1.00  0.00  0.00   144"""
        assert (str_contain_nos(src_cmp_str, result.output))

        homo_cmp_str = """float_100cm_src_FUSE_cREF_mGAIN-BLK-OFFSET_k5_5.tif:
    
            r2   RMSE  rRMSE   N 
    Band 1 1.00  0.00  0.00   144
    Mean   1.00  0.00  0.00   144"""
        assert (str_contain_nos(homo_cmp_str, result.output))

        sum_cmp_str = """File                         Mean r2  Mean RMSE  Mean rRMSE  Mean N
                                    float_100cm_src.tif   1.00      0.00        0.00      144  
    float_100cm_src_FUSE_cREF_mGAIN-BLK-OFFSET_k5_5.tif   1.00      0.00        0.00      144"""
        assert (str_contain_nos(sum_cmp_str, result.output))


def test_compare_file_exists_error(runner, float_100cm_ref_file, float_100cm_src_file):
    """ Test --compare raises an exception when the specified file does not exist. """
    ref_file = float_100cm_ref_file
    src_file = float_100cm_src_file
    # test --compare in flag (no value), and value configurayion
    cli_str = f'fuse  {src_file} {ref_file} --compare unknown.tif'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code != 0)
    assert ('does not exist' in result.output)


@pytest.mark.parametrize('proc_crs', [ProcCrs.auto, ProcCrs.ref, ProcCrs.src])
def test_proc_crs(tmp_path, runner, float_100cm_ref_file, float_100cm_src_file, proc_crs):
    """ Test valid --proc-crs settings generate an output with correct metadata. """
    ref_file = float_100cm_ref_file
    src_file = float_100cm_src_file
    method = Method.gain_blk_offset
    kernel_shape = (3, 3)
    res_proc_crs = ProcCrs.ref if proc_crs == ProcCrs.auto else proc_crs
    post_fix = utils.create_out_postfix(res_proc_crs, method, kernel_shape, RasterFuse.create_out_profile()['driver'])
    homo_file = tmp_path.joinpath(src_file.stem + post_fix)
    cli_str = (
        f'fuse -m {method.value} -k {kernel_shape[0]} {kernel_shape[1]} -od {tmp_path} -pc {proc_crs.value} '
        f'{src_file} {ref_file}'
    )
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)
    assert (homo_file.exists())

    with rio.open(homo_file, 'r') as out_ds:
        assert (out_ds.tags()['FUSE_PROC_CRS'] == res_proc_crs.name)


def test_conf_file(tmp_path, runner, basic_fuse_cli_params):
    """ Test passing a configuration file results in a correctly configured output. """
    # create test configuration file
    conf_dict = dict(
        mask_partial=True, param_image=True, dtype='uint8', nodata=0, creation_options=dict(compress='lzw')
    )
    conf_file = tmp_path.joinpath('conf.yaml')
    with open(conf_file, 'w') as f:
        yaml.dump(conf_dict, f)

    cli_str = basic_fuse_cli_params.cli_str + f' -c {conf_file}'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)
    assert (basic_fuse_cli_params.homo_file.exists())
    assert (basic_fuse_cli_params.param_file.exists())  # test param_image==True

    with rio.open(basic_fuse_cli_params.src_file, 'r') as src_ds:
        with rio.open(basic_fuse_cli_params.homo_file, 'r') as out_ds:
            # test nodata, dtype and creation_options
            assert (out_ds.nodata == conf_dict['nodata'])
            assert (out_ds.dtypes[0] == conf_dict['dtype'])
            assert (out_ds.profile['compress'] == conf_dict['creation_options']['compress'])
            # test mask_partial==True
            src_mask = src_ds.dataset_mask().astype('bool', copy=False)
            out_mask = out_ds.dataset_mask().astype('bool', copy=False)
            assert (src_mask[out_mask].all())
            assert (src_mask.sum() > out_mask.sum())
            # test proc_crs
            assert (out_ds.tags()['FUSE_PROC_CRS'] == basic_fuse_cli_params.proc_crs.name)


def test_param_image(runner, basic_fuse_cli_params):
    """ Test --param-image. """
    # test that cli without --param-image generates no parameter image
    cli_str = basic_fuse_cli_params.cli_str
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)
    assert (basic_fuse_cli_params.homo_file.exists())
    assert (not basic_fuse_cli_params.param_file.exists())

    # test --param-image generates a valid parameter image
    cli_str = basic_fuse_cli_params.cli_str + ' --param-image -o'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)
    assert (basic_fuse_cli_params.homo_file.exists())
    assert (basic_fuse_cli_params.param_file.exists())
    utils.validate_param_image(basic_fuse_cli_params.param_file)


def test_mask_partial(runner, basic_fuse_cli_params):
    """ Test --mask-partial. """
    cli_str = basic_fuse_cli_params.cli_str + ' --mask-partial'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)
    assert (basic_fuse_cli_params.homo_file.exists())

    with rio.open(basic_fuse_cli_params.src_file, 'r') as src_ds:
        with rio.open(basic_fuse_cli_params.homo_file, 'r') as out_ds:
            # test that the output mask is contained by and smaller than the src mask
            src_mask = src_ds.dataset_mask().astype('bool', copy=False)
            out_mask = out_ds.dataset_mask().astype('bool', copy=False)
            assert (src_mask[out_mask].all())
            assert (src_mask.sum() > out_mask.sum())


def test_threads(runner, basic_fuse_cli_params):
    """ Test --threads. """
    cli_str = basic_fuse_cli_params.cli_str + ' --threads 1'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)
    assert (basic_fuse_cli_params.homo_file.exists())


def test_max_block_mem(runner, basic_fuse_cli_params):
    """ Test --max-block-mem. """
    cli_str = basic_fuse_cli_params.cli_str + ' -mbm 1e-4'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)
    assert (basic_fuse_cli_params.homo_file.exists())

    # test that max_block_mem too small raises a BlockSizeError
    cli_str = basic_fuse_cli_params.cli_str + ' -o -mbm 1e-6'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code != 0)
    assert ('BlockSizeError' in result.output)


@pytest.mark.parametrize('upsampling', [r.name for r in rio.warp.SUPPORTED_RESAMPLING])
def test_upsampling(runner, basic_fuse_cli_params, upsampling):
    """ Test --upsampling with valid values generates output with correct metadata. """
    cli_str = basic_fuse_cli_params.cli_str + f' --upsampling {upsampling}'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)
    assert (basic_fuse_cli_params.homo_file.exists())
    with rio.open(basic_fuse_cli_params.homo_file, 'r') as out_ds:
        tags_dict = out_ds.tags()
        assert ('FUSE_UPSAMPLING' in tags_dict)
        assert (yaml.safe_load(tags_dict['FUSE_UPSAMPLING']) == upsampling)


@pytest.mark.parametrize('downsampling', [r.name for r in rio.warp.SUPPORTED_RESAMPLING])
def test_downsampling(runner, basic_fuse_cli_params, downsampling):
    """ Test --downsampling with valid values generates output with correct metadata. """
    cli_str = basic_fuse_cli_params.cli_str + f' --downsampling {downsampling}'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)
    assert (basic_fuse_cli_params.homo_file.exists())
    with rio.open(basic_fuse_cli_params.homo_file, 'r') as out_ds:
        tags_dict = out_ds.tags()
        assert ('FUSE_DOWNSAMPLING' in tags_dict)
        assert (yaml.safe_load(tags_dict['FUSE_DOWNSAMPLING']) == downsampling)


def test_upsampling_error(runner, basic_fuse_cli_params):
    """ Test --upsampling with bad value raises an error. """
    cli_str = basic_fuse_cli_params.cli_str + f' --upsampling unknown'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code != 0)
    assert ("Invalid value for '-us' / '--upsampling'" in result.output)


def test_downsampling_error(runner, basic_fuse_cli_params):
    """ Test --downsampling with bad value raises an error. """
    cli_str = basic_fuse_cli_params.cli_str + f' --downsampling unknown'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code != 0)
    assert ("Invalid value for '-ds' / '--downsampling'" in result.output)


@pytest.mark.parametrize('r2_inpaint_thresh', [0, 0.5, 1])
def test_r2_inpaint_thresh(runner, basic_fuse_cli_params, r2_inpaint_thresh):
    """ Test --r2-inpaint-thresh generates an output with correct metadata. """
    cli_str = basic_fuse_cli_params.cli_str + f' --r2-inpaint-thresh {r2_inpaint_thresh}'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)
    assert (basic_fuse_cli_params.homo_file.exists())
    with rio.open(basic_fuse_cli_params.homo_file, 'r') as out_ds:
        tags_dict = out_ds.tags()
        assert ('FUSE_R2_INPAINT_THRESH' in tags_dict)
        assert (yaml.safe_load(tags_dict['FUSE_R2_INPAINT_THRESH']) == r2_inpaint_thresh)


@pytest.mark.parametrize('bad_r2_inpaint_thresh', [-1, 2])
def test_r2_inpaint_thresh_error(runner, basic_fuse_cli_params, bad_r2_inpaint_thresh):
    """ Test --r2-inpaint-thresh with bad value raises an error. """
    cli_str = basic_fuse_cli_params.cli_str + f' --r2-inpaint-thresh {bad_r2_inpaint_thresh}'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code != 0)
    assert ('Invalid value' in result.output)


@pytest.mark.parametrize(
    'driver, dtype, nodata', [
        ('GTiff', 'float64', float('nan')),
        ('GTiff', 'uint16', 65535),
        ('PNG', 'uint8', 0),
    ]
) # yapf: disable
def test_out_profile(runner, basic_fuse_cli_params, driver, dtype, nodata):
    """ Test --out-* options generate a correctly configured output. """
    cli_str = basic_fuse_cli_params.cli_str + f' --driver {driver} --dtype {dtype} --nodata {nodata}'
    ext_dict = rio.drivers.raster_driver_extensions()
    ext_idx = list(ext_dict.values()).index(driver)
    ext = list(ext_dict.keys())[ext_idx]
    homo_file = basic_fuse_cli_params.homo_file.parent.joinpath(f'{basic_fuse_cli_params.homo_file.stem}.{ext}')
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)
    assert (homo_file.exists())
    with rio.open(homo_file, 'r') as out_ds:
        assert (out_ds.driver == driver)
        assert (out_ds.dtypes[0] == dtype)
        assert (utils.nan_equals(out_ds.nodata, nodata))


def test_out_driver_error(runner, basic_fuse_cli_params):
    """ Test --driver with invalid value raises an error. """
    cli_str = basic_fuse_cli_params.cli_str + f' --driver unk'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code != 0)
    assert ('Invalid value' in result.output)


def test_out_dtype_error(runner, basic_fuse_cli_params):
    """ Test --dtype with invalid value raises an error. """
    cli_str = basic_fuse_cli_params.cli_str + f' --dtype unk'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code != 0)
    assert ('Invalid value' in result.output)


def test_out_nodata_error(runner, basic_fuse_cli_params):
    """ Test --nodata with invalid value (cannot be cast to --dtype) raises an error. """
    cli_str = basic_fuse_cli_params.cli_str + f' --dtype uint8 --nodata nan'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code != 0)
    assert ('Invalid value' in result.output)


def test_creation_options(runner, basic_fuse_cli_params):
    """ Test -co creation options generate correctly configured output. """
    cli_str = basic_fuse_cli_params.cli_str + f' -co COMPRESS=LZW -co PREDICTOR=2 -co TILED=NO'
    result = runner.invoke(cli, cli_str.split())
    assert (result.exit_code == 0)
    assert (basic_fuse_cli_params.homo_file.exists())
    with rio.open(basic_fuse_cli_params.homo_file, 'r') as out_ds:
        assert (out_ds.profile['compress'] == 'lzw')
        assert (not out_ds.profile['tiled'])
