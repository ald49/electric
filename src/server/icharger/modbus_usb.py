import modbus_tk.defines as cst
from modbus_tk.modbus import Query
from modbus_tk.modbus_rtu import RtuMaster
from modbus_tk.exceptions import ModbusInvalidRequestError, ModbusInvalidResponseError

import usb.core
import usb.util
import struct
import sys

ICHARGER_VENDOR_ID = 0x483
ICHARGER_PRODUCT_ID = 0x5751

END_POINT_ADDRESS_WRITE = 0x01
END_POINT_ADDRESS_READ = 0x81

MAX_READWRITE_LEN = 64

READ_REG_COUNT_MAX = 30
WRITE_REG_COUNT_MAX = 28

STATUS_RUN = 0x01
STATUS_ERROR = 0x02
STATUS_CONTROL_STATUS = 0x04
STATUS_RUN_STATUS = 0x08
STATUS_DLG_BOX_STATUS = 0x10
STATUS_CELL_VOLTAGE = 0x20
STATUS_BALANCE = 0x40

#
#
# relies on the following going into /etc/udev/rules.d/10-icharger4010.rules
#
# apply user land permissions so we don't require root to read/write it
# SUBSYSTEMS=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="5751", MODE:="0666"
#
#

class iChargerQuery(Query):
    """
    Subclass of a Query. Adds the Modbus specific part of the protocol for iCharger over USB, which uses
    a rather specific protocol format to send the PDU.

    Please note - read/writes are limited to 64 bytes, whereby the PDU is prefixed with two bytes, <ADU len>
    and 0x30 respectively.

    The 'slave' portion of the protocol goes unused because, I presume, the iCharger provides only a
    single modbus slave - or because of coconuts.
    """

    def __init__(self):
        """Constructor"""
        super(iChargerQuery, self).__init__()
        self.adu_len = None
        self.start_addr = None
        self.quantity = None

    def build_request(self, pdu, slave):
        """ Constructs the output buffer for the request based on the func_code value """
        (self.func_code, ) = struct.unpack(">B", pdu[0])

        if self.func_code == cst.READ_INPUT_REGISTERS:
            self.adu_len = 7
            (self.start_addr, self.quantity) = struct.unpack(">HH", pdu[1:5])
        else:
            raise ModbusInvalidRequestError("Request func code not recognized (code is: {0})".format(self.func_code))

        return struct.pack(">BB", self.adu_len, 0x30) + pdu

    def parse_response(self, response):
        if len(response) < 3:
            raise ModbusInvalidResponseError("Response length is invalid {0}".format(len(response)))

        # check for max length problem, the iCharger HID based Modbus protocol handles only
        # 64 byte packets.  If you want to read more, then send multiple read requests.
        (self.response_length, self.adu_constant, self.response_func_code) = struct.unpack(">BBB", response[0:3])

        if self.adu_constant != 0x30:
            raise ModbusInvalidResponseError("Response doesn't containt constant 0x30 in ADU portion, constant value found is {0}".format(self.adu_constant))

        if self.response_func_code != self.func_code:
            raise ModbusInvalidResponseError("Response func_code {0} isn't the same as the request func_code {1}".format(
                self.response_func_code, self.func_code
            ))

        # primitive byte swap the entire thing...
        header = response[2:4]
        # LOGGER.debug(get_log_buffer("header <- ", header))

        data = response[4:]
        # LOGGER.debug(get_log_buffer("data <- ", data))

        final = header + ''.join([c for t in zip(data[1::2], data[::2]) for c in t])
        # LOGGER.debug(get_log_buffer("final <- ", final))

        return final

class iChargerUSBSerialFacade:
    """
    Implements facade such that the ModBus Master thinks it is using a serial
    device when talking to the iCharger via USB-HID.

    USBSerialFacade sets the active USB configuration and claims the interface,
    take note - this must be released when the instance is cleaned up / destroyed.  If
    the USB device cannot be found the facade does nothing.  If the kernel driver cannot
    be detached that's more of a problem and right now the USBSerialFacade throws a big fat
    exception from __init__.
    """
    def __init__(self, vendorId = ICHARGER_VENDOR_ID, productId = ICHARGER_PRODUCT_ID):
        self._claimed = False
        self.dev = usb.core.find(idVendor=vendorId, idProduct=productId)
        if self.dev is None:
            return

        if not self._detach_kernel_driver():
            sys.exit("failed to detach kernel driver")

        # don't do this - fails every time on the Pi3, regardless of permissions.
        # self.dev.set_configuration()

        self.cfg = self.dev.get_active_configuration()

    def _detach_kernel_driver(self):
        if self.dev.is_kernel_driver_active(0):
            try:
                self.dev.detach_kernel_driver(0)
            except usb.core.USBError as e:
                return False
        return True

    def _claim_interface(self):
        try:
            usb.util.claim_interface(self.dev, 0)
            self._claimed = True
            return True
        except:
            pass
        return False

    def _release_interface(self):
        try:
            usb.util.release_interface(self.dev, 0)
            self._claimed = False
            return True
        except:
            pass
        return False

    @property
    def serial_number(self):
        return usb.util.get_string(self.dev, self.dev.iSerialNumber) if self.valid else None

    @property
    def is_open(self):
        return self.dev is not None and self._claimed

    @property
    def name(self):
        if self.serial_number is not None:
            return "iCharger 4010 Duo SN:" + self.serial_number
        return "! iCharger Not Connected !"

    def open(self):
       return self._claim_interface()

    def close(self):
        return self._release_interface()

    @property
    def timeout(self):
        return 5000

    @timeout.setter
    def timeout(self, new_timeout):
        pass

    @property
    def baudrate(self):
        """As this is a serial facade, we return a totally fake baudrate here"""
        return 19200

    @property
    def valid(self):
        return self.dev is not None

    def reset_input_buffer(self):
        """There are no internal buffers so this method is a no-op"""
        pass

    def reset_output_buffer(self):
        """There are no internal buffers so this method is a no-op"""
        pass

    def write(self, content):
        if self.dev is not None and self._claimed:
            pad_len = MAX_READWRITE_LEN - len(content)
            self.dev.write(END_POINT_ADDRESS_WRITE, content + ("\0" * pad_len))
        return 0

    def read(self, expected_length):
        if self.dev is not None and self._claimed:
            return self.dev.read(END_POINT_ADDRESS_READ, expected_length).tostring()
        return 0


class iChargerMaster(RtuMaster):
    """
    Modbus master interface to the iCharger, implements higher level routines to get the
    status / channel information from the device.
    """
    def __init__(self, serial = None):
        if serial is None:
            serial = iChargerUSBSerialFacade()
        super(iChargerMaster, self).__init__(serial)

    def _make_query(self):
        return iChargerQuery()

    def _modbus_read_input_registers(self, addr, format):
        """
        Uses the modbus_tk framework to acquire data from the device.

        The data_format specifies the layout of the data being returned and is always
        specified in native format - DO NOT include '>' or '<' as then the byte
        swapping will not work.

        The number of words (2 bytes) of data being returned is calculated as the byte size of
        the return packet / 2, and the total length of data to be read is the total byte size
        + 4 bytes for the header information.  This appears to be a Modbus protocol specific
        decision by Junsi for the iCharger.

        :param addr: the base address (offsets are in words not bytes)
        :param format: the structure of the data being received, assumes struct.unpack()
        :return: the tuples of unpacked and byte swapped data
        """
        byte_len = struct.calcsize(format)
        quant = byte_len // 2

        """The slave param (1 in this case) is never used, its appropriate to RTU based Modbus
        devices but as this is iCharger via USB-HID this is irrelevant."""
        return self.execute(1,
                            cst.READ_INPUT_REGISTERS,
                            addr,
                            data_format=format,
                            quantity_of_x=quant,
                            expected_length=(quant * 2) + 4)

    def _status_word_as_json(self, status):
        return {
            "run": status & STATUS_RUN,
            "err": status & STATUS_ERROR,
            "ctrl_status": status & STATUS_CONTROL_STATUS,
            "run_status": status & STATUS_RUN_STATUS,
            "dlg_box_status": status & STATUS_DLG_BOX_STATUS,
            "cell_volt_status": status & STATUS_CELL_VOLTAGE,
            "balance": status & STATUS_BALANCE
        }

    def get_device_info(self):
        """
        Returns the following information from the iCharger, known as the 'device only reads message'
        :return: a tuple containing the response of 'device reads only message'
        Device ID (u16)
        Device SN (S8[12])
        Software Version (u16)
        Hardware Version (u16)
        SYSTEM length (u16 - see also SYSTEM storage area)
        MEMORY length (u16)
        ch1 status word (u16)
        ch2 status word (u16)

        The channel 1/2 status words following this bit-mask:
        Bit0-run flag
        Bit1-error flag
        Bit2-control status flag
        Bit3-run status flag
        Bit4-dialog box status flag
        Bit5-cell voltage flag
        Bit6-balance flag
        """
        data = self._modbus_read_input_registers(0x000, format="h12sHHHHHH")

        return {
            "device_id": data[0],
            "device_sn": data[1],
            "software_ver": data[2],
            "hardware_ver": data[3],
            "system_len": data[4],
            "memory_len": data[5],
            "ch1_status": self._status_word_as_json(data[6]),
            "ch2_status": self._status_word_as_json(data[7])
        }

    def _cell_status_summary(self, cell, voltage, balance, ir):
        return {
            "cell": cell,
            "v": voltage,
            "balance": balance,
            "ir": ir
        }

    def get_channel_status(self, channel):
        """"
        Returns the following information from the iCharger, known as the 'channel input read only' message:
        :return:
        Timestamp (u32)
        The current output power (u32)
        The current output current (s16)
        The current input voltage (u16)
        The current output voltage (u16)
        The current output capacity (s32)
        The current internal temp (s16)
        The current external temp (s16)
        Cell 0-15 voltage (each is u16, 4010DUO uses only first 10)
        Cell 0-15 balance status (each is u8, 4010DUO uses only first 10)
        Cell 0-15 internal resistance (each is u16, 4010DUO uses only first 10)
        The cells total IR (u16)
        Cycle count (u16)
        Control status (u16)
        Run status (u16)
        Run error (u16)
        Dialog Box ID (u16)
        """
        addr = 0x100 if channel == 1 else 0x200

        # timestamp -> cell 0-15 voltage
        data1_fmt = "LLhHHlhh15H"
        data1 = self._modbus_read_input_registers(addr, format=data1_fmt)

        # cell 0-15 balance
        data2_fmt = "8H"
        data2_addr = addr + (struct.calcsize(data1_fmt) / 2)
        cell_balance = self._modbus_read_input_registers(data2_addr, data2_fmt)

        # cell 0-15 IR
        data3_fmt = "15H"
        data3_addr = data2_addr + (struct.calcsize(data2_fmt) / 2)
        cell_ir = self._modbus_read_input_registers(data3_addr, data3_fmt)

        # total IR -> dialog box ID
        data4_fmt = "7H"
        data4_addr = data3_addr + (struct.calcsize(data3_fmt) / 2)
        data4 = self._modbus_read_input_registers(data4_addr, data4_fmt)

        return {
            "channel": channel,
            "timestamp": data1[0],
            "curr_out_power": data1[1],
            "curr_out_amps": data1[2],
            "curr_inp_volts": data1[3],
            "curr_out_volts": data1[4],
            "curr_out_capacity": data1[5],
            "curr_int_temp": data1[6],
            "curr_ext_temp": data1[7],
            "cells": [
                self._cell_status_summary("0", data1[8], cell_balance[0], cell_ir[0]),
                self._cell_status_summary("1", data1[9], cell_balance[1], cell_ir[1]),
                self._cell_status_summary("2", data1[10], cell_balance[2], cell_ir[2]),
                self._cell_status_summary("3", data1[11], cell_balance[3], cell_ir[3]),
                self._cell_status_summary("4", data1[12], cell_balance[4], cell_ir[4]),
                self._cell_status_summary("5", data1[13], cell_balance[5], cell_ir[5]),
                self._cell_status_summary("6", data1[14], cell_balance[6], cell_ir[6]),
                self._cell_status_summary("7", data1[15], cell_balance[7], cell_ir[7]),
                self._cell_status_summary("8", data1[16], cell_balance[8], cell_ir[8]),
                self._cell_status_summary("9", data1[17], cell_balance[9], cell_ir[9]),
            ],
            "cell_total_ir": data4[0],
            "line_intern_res": data4[1],
            "cycle_count": data4[2],
            "control_status": data4[3],
            "run_status": data4[4],
            "run_error": data4[5],
            "dlg_box_id": data4[6]
        }