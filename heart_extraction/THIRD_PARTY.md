# Third-party software record

The diagnostic script contains no copied third-party algorithm implementation.
It calls the public APIs of the packages below. Exact runtime versions are also
written into each output JSON.

| Package | Pinned version | Use | License | Academic/software reference |
|---|---:|---|---|---|
| NumPy | 2.4.4 | Array operations and exact window statistics | BSD-3-Clause | Harris et al. (2020), DOI `10.1038/s41586-020-2649-2` |
| SciPy | 1.17.1 | Integer-PCM and IEEE-float WAV decoding | BSD-3-Clause | Virtanen et al. (2020), DOI `10.1038/s41592-019-0686-2` |
| Pillow | 12.2.0 | PNG raster drawing and metadata | MIT-CMU | Pillow project documentation and source repository |

Primary project and license locations:

- NumPy: <https://numpy.org/> and <https://github.com/numpy/numpy/blob/main/LICENSE.txt>
- SciPy: <https://scipy.org/> and <https://github.com/scipy/scipy/blob/main/LICENSE.txt>
- Pillow: <https://python-pillow.github.io/> and <https://github.com/python-pillow/Pillow/blob/main/LICENSE>

Python standard-library modules are not listed as external package dependencies.
Generated SVG markup is authored by this project and does not use a plotting
library or copied template.
