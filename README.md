# homonim

Radiometric homogenisation of aerial and satellite imagery by fusion with satellite surface reflectance data.  

## Description

`homonim` corrects multi-spectral aerial and satellite imagery to approximate surface reflectance, by fusion with concurrent and collocated satellite surface reflectance data.  It is a form of **spectral harmonisation**, that  adjusts for spatially varying atmospheric and anisotropic (BRDF) effects, without the need for manual reflectance measurements, or target placements.  It can be applied to drone, aerial or satellite imagery.  

`homonim` is based on the method described
in [Radiometric homogenisation of aerial images by calibrating with satellite data](https://www.researchgate.net/publication/328317307_Radiometric_homogenisation_of_aerial_images_by_calibrating_with_satellite_data).   

## Installation
`homonim` is available as a python 3 package, via `pip` and `conda`.  Under Windows, we recommend using `conda` to simplify the installation of binary dependencies.  The [Miniconda](https://docs.conda.io/en/latest/miniconda.html) installation provides a minimal `conda`.
### conda
```shell
$ conda install -c conda-forge homonim
```
### pip
```shell
$ pip install homonim
```
## Quick Start
Homogenise an image with a reference, using the `gain-offset` method, a sliding kernel of 11x11 pixels:
```shell
$ homonim fuse --method gain-offset --kernel-shape 11 11 <path to source image> <path to reference image> 
```
Statistically compare an image, pre- and post-homogenisation, with a reference image:
```shell
$ homonim compare <path to source image> <path to homogenised image> <path to reference image>
```

## Examples
Some image examples.  
## Usage
### Terminology
Harmonisation, homogenisation, fusion.  
### Background
Sliding kernel, kernel sizes, methods, and sub-commands.
### Image preparation
Band-matching, concurrency and co-registration.  geedim.
## fuse
## compare
## stats


## License
`homonim` is licensed under the terms of the [AGPLv3](https://www.gnu.org/licenses/agpl-3.0.en.html).  This project is developed in collaboration with [InnovUS](https://www.innovus.co.za/) at Stellenbosch University, alternative licenses can be arranged by [contacting](mailto:sjdewet@sun.ac.za) them.

## Citation
- Harris, D., Van Niekerk, A., 2019. Radiometric homogenisation of aerial images by calibrating with satellite data. *Int. J. Remote Sens.* **40**, 2623–2647. [https://doi.org/10.1080/01431161.2018.1528404](https://doi.org/10.1080/01431161.2018.1528404). 

## Author
**Dugal Harris** - [dugalh@gmail.com](mailto:dugalh@gmail.com)
