Image preparation
=================

Before correcting, a *reference* image needs to be acquired.  Examples of suitable surface reflectance image collections for the *reference* image are those produced by Landsat, Sentinel-2 and MODIS.

|geedim|_ can be used as a companion tool to ``homonim`` for acquiring *reference* imagery.  It provides command line search, cloud/shadow-free compositing, and download of `Google Earth Engine`_ surface reflectance imagery. Alternatively, there are a number of online platforms providing these images, including the Google_, `Amazon <https://aws.amazon.com/earth/>`_ and `Microsoft <https://planetarycomputer.microsoft.com/catalog>`_ repositories.

For best results, the *reference* and *source* image(s) should be:

* **Concurrent**:  Capture dates are similar.
* **Co-located**:  Accurately co-registered / orthorectified.
* **Spectrally similar**:  Band spectral responses overlap.

The *reference* image bounds should contain those of the *source* image(s), and *source* / *reference* bands should correspond i.e. *reference* band 1 corresponds to *source* band 1, *reference* band 2 corresponds to *source* band 2 etc.  |rasterio|_ and |gdal|_ provide command line tools for re-ordering bands etc. |rasterio|_ is included in the ``homonim`` installation.

The `method formulation <https://www.researchgate.net/publication/328317307_Radiometric_homogenisation_of_aerial_images_by_calibrating_with_satellite_data>`_ assumes *source* images are raw i.e. without colour-balancing, gamma-correction etc adjustments.  Where possible, this assumption should be adhered to.  Adjusted *source* images will still benefit from correction, however.


.. |rasterio| replace:: ``rasterio``
.. |gdal| replace:: ``gdal``
.. |geedim| replace:: ``geedim``
.. _rasterio: https://rasterio.readthedocs.io/en/latest/cli.html
.. _`rasterio docs`: <https://rasterio.readthedocs.io/en/latest/api/rasterio.enums.html#rasterio.enums.Resampling>
.. _gdal: https://gdal.org/programs/index.html
.. _geedim: https://github.com/dugalh/geedim
.. _Google: https://developers.google.com/earth-engine/datasets
.. _`gdal driver`: https://gdal.org/drivers/raster/index.html
.. _`method formulation`: https://www.researchgate.net/publication/328317307_Radiometric_homogenisation_of_aerial_images_by_calibrating_with_satellite_data
.. _methods: `method formulation`_
.. _`Google Earth Engine`: Google_
.. _paper: `method formulation`_