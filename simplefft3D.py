
import ismrmrd
import os
import itertools
import logging
import numpy as np
import numpy.fft as fft
import ctypes
import mrdhelper
from datetime import datetime

# Folder for debug output files
debugFolder = "/tmp/share/debug"

def groups(iterable, predicate):
    group = []
    for item in iterable:
        group.append(item)

        if predicate(item):
            yield group
            group = []


def conditionalGroups(iterable, predicateAccept, predicateFinish):
    group = []
    try:
        for item in iterable:
            if item is None:
                break

            if predicateAccept(item):
                group.append(item)

            if predicateFinish(item):
                yield group
                group = []
    finally:
        iterable.send_close()


def process(connection, config, mrdHeader):
    logging.info("Config: \n%s", config)
    logging.info("MRD Header: \n%s", mrdHeader)

    # Discard phase correction lines and accumulate lines until "ACQ_LAST_IN_SLICE" is set
    for group in conditionalGroups(connection, lambda acq: not acq.is_flag_set(ismrmrd.ACQ_IS_PHASECORR_DATA), lambda acq: acq.is_flag_set(ismrmrd.ACQ_LAST_IN_SLICE)):
        image = process_group(group, config, mrdHeader)

        logging.debug("Sending image to client:\n%s", image)
        connection.send_image(image)


def process_group(group, config, mrdHeader):
    if len(group) == 0:
        return []

    logging.info(f'-------------------------------------------------')
    logging.info(f'     process_group called with {len(group)} readouts')
    logging.info(f'-------------------------------------------------')

    # Create folder, if necessary
    if not os.path.exists(debugFolder):
        os.makedirs(debugFolder)
        logging.debug("Created folder " + debugFolder + " for debug output files")

    # Format data into single [cha RO PE] array
    data = [acquisition.data for acquisition in group]
    data = np.stack(data, axis=-1)
    # Flip matrix in RO/PE to be consistent with ICE
    data = np.flip(data, (1,2))

    logging.debug("Raw data is size %s" % (data.shape,))
    np.save(debugFolder + "/" + "raw.npy", data)

    # Fourier Transforma
    # assume a certain order
    data = data.reshape(data.shape[0],data.shape[1],int(mrdHeader.encoding[0].encodedSpace.matrixSize.y),int(mrdHeader.encoding[0].encodedSpace.matrixSize.z))

    data = fft.fftshift( data, axes=(1, 2, 3))
    data = fft.ifftn(    data, axes=(1, 2, 3))
    data = fft.ifftshift(data, axes=(1, 2, 3))
    data *= np.prod(data.shape) # FFT scaling for consistency with ICE

    # Sum of squares coil combination
    data = np.abs(data)
    data = np.square(data)
    data = np.sum(data, axis=0)
    data = np.sqrt(data)

    logging.debug("Image data is size %s" % (data.shape,))
    np.save(debugFolder + "/" + "img.npy", data)

    # Determine max value (12 or 16 bit)
    BitsStored = 12
    if (mrdhelper.get_userParameterLong_value(mrdHeader, "BitsStored") is not None):
        BitsStored = mrdhelper.get_userParameterLong_value(mrdHeader, "BitsStored")
    maxVal = 2**BitsStored - 1

    # Normalize and convert to int16
    data *= maxVal/data.max()
    data = np.around(data)
    data = data.astype(np.int16)

    # Remove readout oversampling
    if mrdHeader.encoding[0].reconSpace.matrixSize.x != 0:
        offset = int((data.shape[0] - mrdHeader.encoding[0].reconSpace.matrixSize.x)/2)
        data = data[offset:offset+mrdHeader.encoding[0].reconSpace.matrixSize.x,:,:]

    # Remove phase oversampling
    if mrdHeader.encoding[0].reconSpace.matrixSize.y != 0:
        offset = int((data.shape[1] - mrdHeader.encoding[0].reconSpace.matrixSize.y)/2)
        data = data[:,offset:offset+mrdHeader.encoding[0].reconSpace.matrixSize.y,:]

    # Remove phase oversampling 2
    if mrdHeader.encoding[0].reconSpace.matrixSize.z != 0:
        offset = int((data.shape[2] - mrdHeader.encoding[0].reconSpace.matrixSize.z)/2)
        data = data[:,:,offset:offset+mrdHeader.encoding[0].reconSpace.matrixSize.z]

    logging.debug("Image without oversampling is size %s" % (data.shape,))
    np.save(debugFolder + "/" + "imgCrop.npy", data)

    '''
    # Format as ISMRMRD image data
    # data has shape [RO PE], i.e. [x y].
    # from_array() should be called with 'transpose=False' to avoid warnings, and when called
    # with this option, can take input as: [cha z y x], [z y x], or [y x]
    image = ismrmrd.Image.from_array(data.transpose(), acquisition=group[0], transpose=False)
    image.image_index = 1

    # Set field of view
    image.field_of_view = (ctypes.c_float(mrdHeader.encoding[0].reconSpace.fieldOfView_mm.x),
                            ctypes.c_float(mrdHeader.encoding[0].reconSpace.fieldOfView_mm.y),
                            ctypes.c_float(mrdHeader.encoding[0].reconSpace.fieldOfView_mm.z))

    # Set ISMRMRD Meta Attributes
    meta = ismrmrd.Meta({'DataRole':               'Image',
                         'ImageProcessingHistory': ['FIRE', 'PYTHON'],
                         'WindowCenter':           str((maxVal+1)/2),
                         'WindowWidth':            str((maxVal+1))})

    # Add image orientation directions to MetaAttributes if not already present
    if meta.get('ImageRowDir') is None:
        meta['ImageRowDir'] = ["{:.18f}".format(image.getHead().read_dir[0]), "{:.18f}".format(image.getHead().read_dir[1]), "{:.18f}".format(image.getHead().read_dir[2])]

    if meta.get('ImageColumnDir') is None:
        meta['ImageColumnDir'] = ["{:.18f}".format(image.getHead().phase_dir[0]), "{:.18f}".format(image.getHead().phase_dir[1]), "{:.18f}".format(image.getHead().phase_dir[2])]

    xml = meta.serialize()
    logging.debug("Image MetaAttributes: %s", xml)
    logging.debug("Image data has %d elements", image.data.size)

    image.attribute_string = xml
    return image
    '''

    # Format as ISMRMRD image data
    partition = [acquisition.idx.kspace_encode_step_2 for acquisition in group]
    rawHead = [None] * (max(partition) + 1)
    for acq, partition in zip(group, partition):
        rawHead[partition] = acq.getHead()

    imagesOut = [None] * data.shape[-1]
    for partition in range(data.shape[-1]):
        # Create new MRD instance for the processed image
        imagesOut[partition] = ismrmrd.Image.from_array(data[...,partition], transpose=False)

        # Set the header information
        imagesOut[partition].setHead(mrdhelper.update_img_header_from_raw(imagesOut[partition].getHead(), rawHead[partition]))
        imagesOut[partition].field_of_view = (ctypes.c_float(mrdHeader.encoding[0].reconSpace.fieldOfView_mm.x),
                                ctypes.c_float(mrdHeader.encoding[0].reconSpace.fieldOfView_mm.y),
                                ctypes.c_float(mrdHeader.encoding[0].reconSpace.fieldOfView_mm.z))
        imagesOut[partition].slice = partition

        # Set ISMRMRD Meta Attributes
        meta = ismrmrd.Meta({'DataRole': 'Image',
                             'ImageProcessingHistory': ['FIRE', 'PYTHON'],
                             'WindowCenter': str((maxVal + 1) / 2),
                             'WindowWidth': str((maxVal + 1))})

        # Add image orientation directions to MetaAttributes if not already present
        if meta.get('ImageRowDir') is None:
            meta['ImageRowDir'] = ["{:.18f}".format(imagesOut[partition].getHead().read_dir[0]),
                                   "{:.18f}".format(imagesOut[partition].getHead().read_dir[1]),
                                   "{:.18f}".format(imagesOut[partition].getHead().read_dir[2])]

        if meta.get('ImageColumnDir') is None:
            meta['ImageColumnDir'] = ["{:.18f}".format(imagesOut[partition].getHead().phase_dir[0]),
                                      "{:.18f}".format(imagesOut[partition].getHead().phase_dir[1]),
                                      "{:.18f}".format(imagesOut[partition].getHead().phase_dir[2])]

        xml = meta.serialize()
        logging.debug("Image MetaAttributes: %s", xml)
        logging.debug("Image data has %d elements", imagesOut[partition].data.size)

        imagesOut[partition].attribute_string = xml

    return imagesOut


