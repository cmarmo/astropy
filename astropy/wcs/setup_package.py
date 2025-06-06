# Licensed under a 3-clause BSD style license - see LICENSE.rst

import io
import os
import os.path
import shutil
import sys
from collections import defaultdict
from os.path import join
from pathlib import Path

from numpy import get_include as get_numpy_include
from setuptools import Extension

from extension_helpers import get_compiler, import_file, pkg_config, write_if_different

WCSROOT = os.path.relpath(os.path.dirname(__file__))
WCSVERSION = "8.4"


def b(s):
    return s.encode("ascii")


def string_escape(s):
    s = s.decode("ascii").encode("ascii", "backslashreplace")
    s = s.replace(b"\n", b"\\n")
    s = s.replace(b"\0", b"\\0")
    return s.decode("ascii")


def determine_64_bit_int():
    """
    The only configuration parameter needed at compile-time is how to
    specify a 64-bit signed integer.  Python's ctypes module can get us
    that information.
    If we can't be absolutely certain, we default to "long long int",
    which is correct on most platforms (x86, x86_64).  If we find
    platforms where this heuristic doesn't work, we may need to
    hardcode for them.
    """
    try:
        try:
            import ctypes
        except ImportError:
            raise ValueError()

        if ctypes.sizeof(ctypes.c_longlong) == 8:
            return "long long int"
        elif ctypes.sizeof(ctypes.c_long) == 8:
            return "long int"
        elif ctypes.sizeof(ctypes.c_int) == 8:
            return "int"
        else:
            raise ValueError()

    except ValueError:
        return "long long int"


def write_wcsconfig_h(paths):
    """
    Writes out the wcsconfig.h header with local configuration.
    """
    h_file = io.StringIO()
    h_file.write(
        f"""
    /* The bundled version has WCSLIB_VERSION */
    #define HAVE_WCSLIB_VERSION 1

    /* WCSLIB library version number. */
    #define WCSLIB_VERSION {WCSVERSION}

    /* 64-bit integer data type. */
    #define WCSLIB_INT64 {determine_64_bit_int()}

    /* Windows needs some other defines to prevent inclusion of wcsset()
       which conflicts with wcslib's wcsset().  These need to be set
       on code that *uses* astropy.wcs, in addition to astropy.wcs itself.
       */
    #if defined(_WIN32) || defined(_MSC_VER) || defined(__MINGW32__) || defined (__MINGW64__)

    #ifndef YY_NO_UNISTD_H
    #define YY_NO_UNISTD_H
    #endif

    #ifndef _CRT_SECURE_NO_WARNINGS
    #define _CRT_SECURE_NO_WARNINGS
    #endif

    #ifndef _NO_OLDNAMES
    #define _NO_OLDNAMES
    #endif

    #ifndef NO_OLDNAMES
    #define NO_OLDNAMES
    #endif

    #ifndef __STDC__
    #define __STDC__ 1
    #endif

    #endif
    """
    )
    content = h_file.getvalue().encode("ascii")
    for path in paths:
        write_if_different(path, content)


######################################################################
# GENERATE DOCSTRINGS IN C


def generate_c_docstrings():
    docstrings = import_file(os.path.join(WCSROOT, "docstrings.py"))
    docstrings = docstrings.__dict__
    keys = [
        key for key, val in docstrings.items()
        if not key.startswith("__") and isinstance(val, str)
    ]  # fmt: skip
    keys.sort()
    docs = {}
    for key in keys:
        docs[key] = docstrings[key].encode("utf8").lstrip() + b"\0"

    h_file = io.StringIO()
    h_file.write(
        """/*
DO NOT EDIT!

This file is autogenerated by astropy/wcs/setup_package.py.  To edit
its contents, edit astropy/wcs/docstrings.py
*/

#ifndef __DOCSTRINGS_H__
#define __DOCSTRINGS_H__

"""
    )
    for key in keys:
        val = docs[key]
        h_file.write(f"extern char doc_{key}[{len(val)}];\n")
    h_file.write("\n#endif\n\n")

    write_if_different(
        join(WCSROOT, "include", "astropy_wcs", "docstrings.h"),
        h_file.getvalue().encode("utf-8"),
    )

    c_file = io.StringIO()
    c_file.write(
        """/*
DO NOT EDIT!

This file is autogenerated by astropy/wcs/setup_package.py.  To edit
its contents, edit astropy/wcs/docstrings.py

The weirdness here with strncpy is because some C compilers, notably
MSVC, do not support string literals greater than 256 characters.
*/

#include <string.h>
#include "astropy_wcs/docstrings.h"

"""
    )
    for key in keys:
        val = docs[key]
        c_file.write(f"char doc_{key}[{len(val)}] = {{\n")
        for i in range(0, len(val), 12):
            section = val[i : i + 12]
            c_file.write("    ")
            c_file.write("".join(f"0x{x:02x}, " for x in section))
            c_file.write("\n")

        c_file.write("    };\n\n")

    write_if_different(
        join(WCSROOT, "src", "docstrings.c"), c_file.getvalue().encode("utf-8")
    )


def get_wcslib_cfg(cfg, wcslib_files, include_paths):
    debug = "--debug" in sys.argv

    cfg["include_dirs"].append(get_numpy_include())
    cfg["define_macros"].extend(
        [
            ("ECHO", None),
            ("WCSTRIG_MACRO", None),
            ("ASTROPY_WCS_BUILD", None),
            ("_GNU_SOURCE", None),
        ]
    )

    if (
        int(os.environ.get("ASTROPY_USE_SYSTEM_WCSLIB", "0"))
        or int(os.environ.get("ASTROPY_USE_SYSTEM_ALL", "0"))
    ) and not sys.platform == "win32":
        wcsconfig_h_path = join(WCSROOT, "include", "wcsconfig.h")
        if os.path.exists(wcsconfig_h_path):
            os.unlink(wcsconfig_h_path)
        for k, v in pkg_config(["wcslib"], ["wcs"]).items():
            cfg[k].extend(v)
    else:
        write_wcsconfig_h(include_paths)

        wcslib_path = join("cextern", "wcslib")  # Path to wcslib
        wcslib_cpath = join(wcslib_path, "C")  # Path to wcslib source files
        cfg["sources"].extend(join(wcslib_cpath, x) for x in wcslib_files)
        cfg["include_dirs"].append(wcslib_cpath)

    if debug:
        cfg["define_macros"].append(("DEBUG", None))
        cfg["undef_macros"].append("NDEBUG")
        if not sys.platform.startswith("sun") and not sys.platform == "win32":
            cfg["extra_compile_args"].extend(["-fno-inline", "-O0", "-g"])
    else:
        # Define ECHO as nothing to prevent spurious newlines from
        # printing within the libwcs parser
        cfg["define_macros"].append(("NDEBUG", None))
        cfg["undef_macros"].append("DEBUG")

    if sys.platform == "win32":
        # These are written into wcsconfig.h, but that file is not
        # used by all parts of wcslib.
        cfg["define_macros"].extend(
            [
                ("YY_NO_UNISTD_H", None),
                ("_CRT_SECURE_NO_WARNINGS", None),
                ("_NO_OLDNAMES", None),  # for mingw32
                ("NO_OLDNAMES", None),  # for mingw64
                ("__STDC__", None),  # for MSVC
            ]
        )

    if sys.platform.startswith("linux"):
        cfg["define_macros"].append(("HAVE_SINCOS", None))

    # For 4.7+ enable C99 syntax in older compilers (need 'gnu99' std for gcc)
    if get_compiler() == "unix":
        cfg["extra_compile_args"].extend(["-std=gnu99"])

    # Squelch a few compilation warnings in WCSLIB
    if get_compiler() in ("unix", "mingw32"):
        if not debug:
            cfg["extra_compile_args"].extend(
                [
                    "-Wno-strict-prototypes",
                    "-Wno-unused-function",
                    "-Wno-unused-value",
                    "-Wno-uninitialized",
                ]
            )


def get_extensions():
    generate_c_docstrings()

    ######################################################################
    # DISTUTILS SETUP
    cfg = defaultdict(list)

    wcslib_files = [  # List of wcslib files to compile
        "flexed/wcsbth.c",
        "flexed/wcspih.c",
        "flexed/wcsulex.c",
        "flexed/wcsutrn.c",
        "cel.c",
        "dis.c",
        "lin.c",
        "log.c",
        "prj.c",
        "spc.c",
        "sph.c",
        "spx.c",
        "tab.c",
        "wcs.c",
        "wcserr.c",
        "wcsfix.c",
        "wcshdr.c",
        "wcsprintf.c",
        "wcsunits.c",
        "wcsutil.c",
    ]

    wcslib_config_paths = [
        join(WCSROOT, "include", "astropy_wcs", "wcsconfig.h"),
        join(WCSROOT, "include", "wcsconfig.h"),
    ]

    get_wcslib_cfg(cfg, wcslib_files, wcslib_config_paths)

    cfg["include_dirs"].append(join(WCSROOT, "include"))

    astropy_wcs_files = [  # List of astropy.wcs files to compile
        "distortion.c",
        "distortion_wrap.c",
        "docstrings.c",
        "pipeline.c",
        "pyutil.c",
        "astropy_wcs.c",
        "astropy_wcs_api.c",
        "sip.c",
        "sip_wrap.c",
        "str_list_proxy.c",
        "unit_list_proxy.c",
        "util.c",
        "wcslib_wrap.c",
        "wcslib_auxprm_wrap.c",
        "wcslib_prjprm_wrap.c",
        "wcslib_celprm_wrap.c",
        "wcslib_tabprm_wrap.c",
        "wcslib_wtbarr_wrap.c",
    ]
    cfg["sources"].extend(join(WCSROOT, "src", x) for x in astropy_wcs_files)

    cfg["sources"] = [str(x) for x in cfg["sources"]]
    cfg = {str(key): val for key, val in cfg.items()}

    # Copy over header files from WCSLIB into the installed version of Astropy
    # so that other Python packages can write extensions that link to it. We
    # do the copying here then include the data in [tools.setuptools.package_data]
    # in the pyproject.toml file

    wcslib_headers = [
        "cel.h",
        "lin.h",
        "prj.h",
        "spc.h",
        "spx.h",
        "tab.h",
        "wcs.h",
        "wcserr.h",
        "wcsmath.h",
        "wcsprintf.h",
    ]

    if not (
        int(os.environ.get("ASTROPY_USE_SYSTEM_WCSLIB", "0"))
        or int(os.environ.get("ASTROPY_USE_SYSTEM_ALL", "0"))
    ):
        for header in wcslib_headers:
            source = Path("cextern", "wcslib", "C", header)
            dest = Path("astropy", "wcs", "include", "wcslib", header)
            if not dest.is_file() or source.stat().st_mtime > dest.stat().st_mtime:
                shutil.copy(source, dest)

    return [Extension("astropy.wcs._wcs", **cfg)]
