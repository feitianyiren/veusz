#    Copyright (C) 2013 Jeremy S. Sanders
#    Email: Jeremy Sanders <jeremy@jeremysanders.net>
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License along
#    with this program; if not, write to the Free Software Foundation, Inc.,
#    51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
##############################################################################

from __future__ import division, print_function

import collections
import re
import sys

import numpy as N
from .. import qtall as qt4
from ..compat import citems, ckeys, cvalues, cstr
from .. import document
from .. import utils
from . import base

def _(text, disambiguation=None, context="Import_HDF5"):
    return qt4.QCoreApplication.translate(context, text, disambiguation)

h5py = None
def inith5py():
    global h5py
    try:
        import h5py
    except ImportError:
        raise RuntimeError(
            "Cannot load Python h5py module. "
            "Please install before loading documents using HDF5 data.")

def filterAttrsByName(attrs, name):
    """For compound datasets, attributes can be given on a per-column basis.
    This filters the attributes by the column name."""

    name = name.strip()
    attrsout = {}
    for a in attrs:
        # attributes with _dsname suffixes are copied
        if a[:4] == "vsz_" and a[-len(name)-1:] == "_"+name:
            attrsout[a[:-len(name)-1]] = attrs[a]
    return attrsout

def convertTextToSlice(slicetxt, numdims):
    """Convert a value like 0:1:3,:,::-1 to a tuple slice
    ((0,1,3), (None, None, None), (None, None, -1))
    or reduce dimensions such as :,3 -> ((None,None,None),3)

    Also checks number of dimensions (including reduced) is numdims.

    Return -1 on error
    """

    if slicetxt.strip() == '':
        return None

    slicearray = slicetxt.split(',')
    if len(slicearray) != numdims:
        # slice needs same dimensions as data
        return -1

    allsliceout = []
    for sliceap_idx, sliceap in enumerate(slicearray):
        sliceparts = sliceap.strip().split(':')

        if len(sliceparts) == 1:
            # reduce dimensions with single index
            try:
                allsliceout.append(int(sliceparts[0]))
            except ValueError:
                # invalid index
                return -1
        elif len(sliceparts) not in (2, 3):
            return -1
        else:
            sliceout = []
            for p in sliceparts:
                p = p.strip()
                if not p:
                    sliceout.append(None)
                else:
                    try:
                        sliceout.append(int(p))
                    except ValueError:
                        return -1
            if len(sliceout) == 2:
                sliceout.append(None)
            allsliceout.append(tuple(sliceout))

    allempty = True
    for s in allsliceout:
        if s != (None, None, None):
            allempty = False
    if allempty:
        return None

    return tuple(allsliceout)

def convertSliceToText(slice):
    """Convert tuple slice into text."""
    if slice is None:
        return ''
    out = []
    for spart in slice:
        if isinstance(spart, int):
            # single index
            out.append(str(spart))
            continue

        sparttxt = []
        for p in spart:
            if p is not None:
                sparttxt.append(str(p))
            else:
                sparttxt.append('')
        if sparttxt[-1] == '':
            del sparttxt[-1]
        out.append(':'.join(sparttxt))
    return ', '.join(out)

def applySlices(data, slices):
    """Given hdf/numpy dataset, apply slicing tuple to it and return data."""
    slist = []
    for s in slices:
        if isinstance(s, int):
            slist.append(s)
        else:
            slist.append(slice(*s))
            if s[2] < 0:
                # negative slicing doesn't work in h5py, so we
                # make a copy
                data = N.array(data)
    try:
        data = data[tuple(slist)]
    except (ValueError, IndexError):
        data = N.array([], dtype=N.float64)
    return data

def convertDatasetToObject(data, slices):
    """Convert numpy/hdf dataset to suitable data for veusz.
    Raise _ConvertError if cannot."""

    if slices:
        data = applySlices(data, slices)

    kind = data.dtype.kind
    if kind in ('b', 'i', 'u', 'f'):
        data = N.array(data, dtype=N.float64)
        if len(data.shape) > 2:
            raise _ConvertError(_("HDF5 dataset has more than 2 dimensions"))
        return data

    elif kind in ('S', 'a') or (
        kind == 'O' and h5py.check_dtype(vlen=data.dtype)):
        if len(data.shape) != 1:
            raise _ConvertError(_("HDF5 dataset has more than 1 dimension"))

        strcnv = list(data)
        return strcnv

    raise _ConvertError(_("HDF5 dataset has an invalid type"))

class ImportParamsHDF5(base.ImportParamsBase):
    """HDF5 file import parameters.

    Additional parameters:
     items: list of datasets and items to import
     namemap: map hdf datasets to veusz names
     slices: dict to map hdf names to slices
     twodranges: map hdf names to 2d range (minx, miny, maxx, maxy)
     twod_as_oned: set of hdf names to read 2d dataset as 1d dataset
     convert_datetime: map float or strings to datetime
    """

    defaults = {
        'items': None,
        'namemap': None,
        'slices': None,
        'twodranges': None,
        'twod_as_oned': None,
        'convert_datetime': None,
        }
    defaults.update(base.ImportParamsBase.defaults)

class LinkedFileHDF5(base.LinkedFileBase):
    """Links a HDF5 file to the data."""

    def createOperation(self):
        """Return operation to recreate self."""
        return OperationDataImportHDF5

    def saveToFile(self, fileobj, relpath=None):
        """Save the link to the document file."""

        p = self.params
        args = [ utils.rrepr(self._getSaveFilename(relpath)),
                 utils.rrepr(p.items) ]
        for k in ('namemap', 'slices', 'twodranges', 'twod_as_oned',
                  'convert_datetime',
                  'prefix', 'suffix'):
            if getattr(p, k):
                args.append("%s=%s" % (k, utils.rrepr(getattr(p, k))) )
        args.append("linked=True")
        fileobj.write("ImportFileHDF5(%s)\n" % ", ".join(args))

class _ConvertError(RuntimeError):
    pass

class _DataRead:
    """Data read from file during import.

    This is so we can store the original name and options stored in
    attributes from the file.
    """
    def __init__(self, origname, data, options):
        self.origname = origname
        self.data = data
        self.options = options

class OperationDataImportHDF5(base.OperationDataImportBase):
    """Import 1d or 2d data from a fits file."""

    descr = _("import HDF5 file")

    def readDataset(self, dataset, dsattrs, dsname, dsread):
        """Given hdf5 dataset, its attributes and name, get data and
        set it in dict dsread.

        dsread maps names to _DataRead object
        """

        # find name for dataset
        if (self.params.namemap is not None and
            dsname in self.params.namemap ):
            name = self.params.namemap[dsname]
        else:
            if "vsz_name" in dsattrs:
                # override name using attribute
                name = dsattrs["vsz_name"]
            else:
                name = dsname.split("/")[-1].strip()
        if name in dsread:
            name = dsname.strip()

        # store options associated with dataset
        options = {}
        for a in ckeys(dsattrs):
            if a[:4] == "vsz_":
                options[a] = dsattrs[a]

        try:
            # implement slicing
            aslice = None
            if "vsz_slice" in dsattrs:
                s = convertTextToSlice(dsattrs["vsz_slice"],
                                       dataset.shape)
                if s != -1:
                    aslice = s
            if self.params.slices and dsname in self.params.slices:
                aslice = self.params.slices[dsname]

            # finally return data
            objdata = convertDatasetToObject(dataset, aslice)
            dsread[name] = _DataRead(dsname, objdata, options)

        except _ConvertError:
            pass

    def walkFile(self, item, dsread):
        """Walk an hdf file, adding datasets to dsread."""

        if isinstance(item, h5py.Dataset):
            if item.dtype.kind == 'V':
                # compound dataset - walk columns
                for name in item.dtype.names:
                    attrs = filterAttrsByName(item.attrs, name)
                    self.readDataset(item[name], attrs, item.name+"/"+name, dsread)
            else:
                self.readDataset(item, item.attrs, item.name, dsread)

        elif isinstance(item, h5py.Group):
            for dsname in sorted(item.keys()):
                try:
                    child = item[dsname]
                except KeyError:
                    # this does happen!
                    continue
                self.walkFile(child, dsread)

    def readDataFromFile(self):
        """Read data from hdf5 file and return a dict of names to data."""

        dsread = {}
        with h5py.File(self.params.filename) as hdff:
            for hi in self.params.items:
                node = hdff
                # lookup group/dataset in file
                for namepart in [x for x in hi.split("/") if x != ""]:
                    # using unicode column names does not work!
                    try:
                        namepart = str(namepart)
                    except ValueError:
                        pass
                    node = node[namepart]

                self.walkFile(node, dsread)
        return dsread

    def collectErrorDatasets(self, dsread):
        """Identify error bar datasets and separate out.
        Returns error bar datasets."""

        # separate out datasets with error bars
        # this a defaultdict of defaultdict with None as default
        errordatasets = collections.defaultdict(
            lambda: collections.defaultdict(lambda: None))
        for name in list(ckeys(dsread)):
            dr = dsread[name]
            ds = dr.data
            if not isinstance(ds, N.ndarray) or len(ds.shape) != 1:
                # skip non-numeric or 2d datasets
                continue

            for err in ('+', '-', '+-'):
                ln = len(err)+3
                if name[-ln:] == (' (%s)' % err):
                    refname = name[:-ln].strip()
                    if refname in dsread:
                        errordatasets[refname][err] = ds
                        del dsread[name]
                        break

        return errordatasets

    def numericDataToDataset(self, name, dread, errordatasets):
        """Convert numeric data to a veusz dataset."""

        data = dread.data

        if len(data.shape) == 1:
            if ( (self.params.convert_datetime and
                  dread.origname in self.params.convert_datetime) or
                 "vsz_convert_datetime" in dread.options ):

                try:
                    mode = self.params.convert_datetime[dread.origname]
                except (TypeError, KeyError):
                    mode = dread.options["vsz_convert_datetime"]

                if mode == 'unix':
                    data = utils.floatUnixToVeusz(data)
                ds = document.DatasetDateTime(data)

            else:
                # Standard 1D Import
                # handle any possible error bars
                args = { 'data': data,
                         'serr': errordatasets[name]['+-'],
                         'nerr': errordatasets[name]['-'],
                         'perr': errordatasets[name]['+'] }

                # find minimum length and cut down if necessary
                minlen = min([len(d) for d in cvalues(args)
                              if d is not None])
                for a in list(ckeys(args)):
                    if args[a] is not None and len(args[a]) > minlen:
                        args[a] = args[a][:minlen]

                ds = document.Dataset(**args)

        elif len(data.shape) == 2:
            # 2D dataset
            if ( ((self.params.twod_as_oned and
                   dread.origname in self.params.twod_as_oned) or
                  "vsz_twod_as_oned" in dread.options) and
                 data.shape[1] in (2,3) ):
                # actually a 1D dataset in disguise
                if data.shape[1] == 2:
                    ds = document.Dataset(data=data[:,0], serr=data[:,1])
                else:
                    ds = document.Dataset(
                        data=data[:,0], perr=data[:,1], nerr=data[:,2])
            else:
                # this really is a 2D dataset
                # find any ranges
                rangex = rangey = None

                if "vsz_range" in dread.options:
                    r = dread.options["vsz_range"]
                    rangex = (r[0], r[2])
                    rangey = (r[1], r[3])
                if ( self.params.twodranges and
                     dread.origname in self.params.twodranges ):
                    r = self.params.twodranges[dread.origname]
                    rangex = (r[0], r[2])
                    rangey = (r[1], r[3])

                # create the object
                ds = document.Dataset2D(data, xrange=rangex, yrange=rangey)

        return ds

    def textDataToDataset(self, name, dread):
        """Convert textual data to a veusz dataset."""

        data = dread.data

        if ( (self.params.convert_datetime and
              dread.origname in self.params.convert_datetime) or
             "vsz_convert_datetime" in dread.options ):

            try:
                fmt = self.params.convert_datetime[dread.origname]
            except (TypeError, KeyError):
                fmt = dread.options["vsz_convert_datetime"]

            try:
                datere = re.compile(utils.dateStrToRegularExpression(fmt))
            except Exception:
                raise base.ImportingError(
                    _("Could not interpret date-time syntax '%s'") % fmt)

            dout = N.empty(len(data), dtype=N.float64)
            for i, ditem in enumerate(data):
                try:
                    match = datere.match(ditem)
                    val = utils.dateREMatchToDate(match)
                except ValueError:
                    val = N.nan
                dout[i] = val

            ds = document.DatasetDateTime(dout)

        else:
            # standard text dataset
            ds = document.DatasetText(dread.data)

        return ds

    def doImport(self, doc):
        """Do the import."""

        inith5py()
        par = self.params

        dsread = self.readDataFromFile()

        # find datasets which are error datasets
        errordatasets = self.collectErrorDatasets(dsread)

        if par.linked:
            linkedfile = LinkedFileHDF5(par)
        else:
            linkedfile = None

        # create the veusz output datasets
        for name, dread in citems(dsread):
            if isinstance(dread.data, N.ndarray):
                # numeric
                ds = self.numericDataToDataset(name, dread, errordatasets)
            else:
                # text
                ds = self.textDataToDataset(name, dread)

            if ds is None:
                # error above
                continue

            ds.linked = linkedfile

            # finally set dataset in document
            fullname = par.prefix + name + par.suffix
            doc.setData(fullname, ds)
            self.outdatasets.append(fullname)

        return list(dsread.keys())

def ImportFileHDF5(comm, filename,
                   items,
                   namemap=None,
                   slices=None,
                   twodranges=None,
                   twod_as_oned=None,
                   convert_datetime=None,
                   prefix='', suffix='',
                   linked=False):
    """Import data from a HDF5 file

    items is a list of groups and datasets which can be imported.
    If a group is imported, all child datasets are imported.

    namemap maps an input dataset to a veusz dataset name. Special
    suffixes can be used on the veusz dataset name to indicate that
    the dataset should be imported specially.

    'foo (+)': import as +ve error for dataset foo
    'foo (-)': import as -ve error for dataset foo
    'foo (+-)': import as symmetric error for dataset foo

    slices is an optional dict specifying slices to be selected when
    importing. For each dataset to be sliced, provide a tuple of
    values, one for each dimension. The values should be a single
    integer to select that index, or a tuple (start, stop, step),
    where the entries are integers or None.

    twodranges is an optional dict giving data ranges for 2d
    datasets. It maps names to (minx, miny, maxx, maxy).

    twod_as_oned: optional set containing 2d datasets to attempt to
    read as 1d

    convert_datetime should be a dict mapping hdf name to specify
    date/time importing
      for a 1d numeric dataset
        if this is set to 'veusz', this is the number of seconds since
          2009-01-01
        if this is set to 'unix', this is the number of seconds since
          1970-01-01
       for a text dataset, this should give the format of the date/time,
          e.g. 'YYYY-MM-DD|T|hh:mm:ss'
 
    linked specifies that the dataset is linked to the file.

    Attributes can be used in datasets to override defaults:
     'vsz_name': set to override name for dataset in veusz
     'vsz_slice': slice on importing (use format "start:stop:step,...")
     'vsz_range': should be 4 item array to specify x and y ranges:
                  [minx, miny, maxx, maxy]
     'vsz_twod_as_oned': treat 2d dataset as 1d dataset with errors
     'vsz_convert_datetime': treat as date/time, set to one of the values
                             above.
 
    For compound datasets these attributes can be given on a
    per-column basis using attribute names
    vsz_attributename_columnname.
    """

    # lookup filename
    realfilename = comm.findFileOnImportPath(filename)
    params = ImportParamsHDF5(
        filename=realfilename,
        items=items,
        namemap=namemap,
        slices=slices,
        twodranges=twodranges,
        twod_as_oned=twod_as_oned,
        convert_datetime=convert_datetime,
        prefix=prefix, suffix=suffix,
        linked=linked)
    op = OperationDataImportHDF5(params)
    comm.document.applyOperation(op)

document.registerImportCommand("ImportFileHDF5", ImportFileHDF5)