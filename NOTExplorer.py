#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# see https://www.gnu.org/licenses/ for license terms
#
# Nevoton Opentherm Explorer utility for Wirenboard
# $Id: NOTExplorer.py 339 2023-03-19 11:03:48Z maxwolf $
# Copyright (C) 2023 MaxWolf 85530cf1e8ef7648e13e7ba2fce87337cbc904e757c83dde1b0d02ecb1508fb7
#
#
#
import argparse
import random
import sys
import time
import threading
import queue
import collections
import logging
import re
from abc import ABC, abstractmethod, abstractproperty

from pymodbus.client.sync import ModbusSerialClient as ModbusClient
import paho.mqtt.client as mqtt


#
# Nevoton module (FW1.3) docs https://nevoton.ru/docs/instructions/OpenTherm_Modbus_BCG-1.0.2-W.pdf
# Opentherm protocol v2.2 https://ihormelnyk.com/Content/Pages/opentherm_library/Opentherm%20Protocol%20v2-2.pdf
# ModBus https://en.wikipedia.org/wiki/Modbus + https://www.modbustools.com/modbus.html
# paho mqtt client docs https://pypi.org/project/paho-mqtt
#


######
#
#
#
######
def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

######
#
# Opentherm defines
#
######
# Message types according to Opentherm specs
OT_READ_DATA=0
OT_WRITE_DATA=1
OT_INVALID_DATA=2
OT_RESERVED=3
OT_READ_ACK=4
OT_WRITE_ACK=5
OT_DATA_INVALID=6
OT_UNKNOWN_DATA_ID=7

# mqtt connect timeout
MQTT_CONN_TIMEOUT=10

######
#
# Nevoton's mqtt direct/transparent opentherm commands (for modbus holding reg 0xD1 aka 209 aka 'TR Command')
#
######
NCMD_READ=2
NCMD_WRITE=3

# 'TR Command' validation error response
NCMD_VAL_ERROR=1

######
#
# Nevoton's mqtt topics
#
######
DEV_PREF="/devices"
NCTL_SUFFIX="/on"
NCTL_ALL="/controls"
# modbus holding reg 0xD1 aka 209
NCTL_COMMAND="/controls/TR Command"
# modbus holding reg 0xD2 aka 210
NCTL_ID="/controls/TR ID"
# modbus holding reg 0xD3 aka 211
NCTL_DATA="/controls/TR Data"

# Nevoton's driver should reply over mqtt in specified number of seconds
MQTT_ReplyTimeout=3
# Opentherm slave device should respond in specified number of seconds (must be more than MQTT_ReplyTimeout!!!; nevoton's circular polling+ModbusRTU+WB serial interface impose such a big )
MQTT_ResponseTimeout=60
# Assume Opentherm slave device reponded with the same data (should be less than MQTT_ResponseTimeout)
MQTT_PartResponseTimeout=30
# Max retries on read
MQTT_ReadRetries=5
# Max retries on write
MQTT_WriteRetries=5

# Nevoton's module should provide opentherm slave device response in specified number of seconds
Modbus_ResponseTimeout=20

######
#
# Decode/comment Opentherm data
#
######
class OTData:
    def __init__(self, data_id, t_dir, pos, fmt, min, max, units, descr):
        self.data_id = data_id  # OT data-id
        self.t_dir = t_dir      # transfer direction ('R' - from slave, 'W' - to slave, 'RW' - both, 
                                # 'I' - from slave special mode (with simulaneous write), O - to slave special mode (with simultaneous read))
        self.pos = pos          # bit position, if exists (HB0 - bit 0 of high byte; LB1 - bit 1 of low byte etc; 0-7 - bits of the whole 16-bit word)
        self.fmt = fmt          # data format: BF - bitfield, U8, U16, S8, S16, F8.8
        self.min = min          # min value
        self.max = max          # max value
        self.units = units      # value units
        self.descr = descr      # data description (could contains conditional (==/>=/<=) descriptions

class OTDecoder:
    def __init__(self):
        self.OT_MSGS = { 
            OT_READ_DATA: "READ-DATA",
            OT_WRITE_DATA: "WRITE-DATA",
            OT_INVALID_DATA: "INVALID-DATA",
            OT_RESERVED: "OT-RESERVED",
            OT_READ_ACK:"READ-ACK", 
            OT_WRITE_ACK:"WRITE-ACK", 
            OT_DATA_INVALID:"DATA-INVALID", 
            OT_UNKNOWN_DATA_ID:"UNKNOWN-DATAID" }
        # Opentherm DATA-ID descriptions collected from 
        #   Opentherm%20Protocol%20v2-2.pdf
        #   https://www.opentherm.eu/request-details
        #   http://otgw.tclcode.com/firmware.html
        #   +forums and other sources...
        self.otd = collections.OrderedDict()
        self.otd["000"]      = OTData("000", "RI", "",    "BF", 0, 1, "", "Master/slave status")
        self.otd["000I"]     = OTData("000", "R", "",    "BF", 0, 1, "", "Master/slave status")
        self.otd["000I:HB0"] = OTData("000", "R", "HB0", "BF", 0, 1, "", "Master status: CH enable")
        self.otd["000I:HB1"] = OTData("000", "R", "HB1", "BF", 0, 1, "", "Master status: DHW enable")
        self.otd["000I:HB2"] = OTData("000", "R", "HB2", "BF", 0, 1, "", "Master status: Cooling enable")
        self.otd["000I:HB3"] = OTData("000", "R", "HB3", "BF", 0, 1, "", "Master status: OTC active")
        self.otd["000I:HB4"] = OTData("000", "R", "HB4", "BF", 0, 1, "", "Master status: CH2 enable")
        self.otd["000I:HB5"] = OTData("000", "R", "HB5", "BF", 0, 1, "", "Master status: Summer/winter mode")
        self.otd["000I:HB6"] = OTData("000", "R", "HB6", "BF", 0, 1, "", "Master status: DHW blocking")
        self.otd["000I:HB7"] = OTData("000", "R", "HB7", "BF", 0, 1, "", "Master status: reserved")
        self.otd["000:HB0"]  = OTData("000", "R", "HB0", "BF", 0, 1, "", "Master status: CH enable")
        self.otd["000:HB1"]  = OTData("000", "R", "HB1", "BF", 0, 1, "", "Master status: DHW enable")
        self.otd["000:HB2"]  = OTData("000", "R", "HB2", "BF", 0, 1, "", "Master status: Cooling enable")
        self.otd["000:HB3"]  = OTData("000", "R", "HB3", "BF", 0, 1, "", "Master status: OTC active")
        self.otd["000:HB4"]  = OTData("000", "R", "HB4", "BF", 0, 1, "", "Master status: CH2 enable")
        self.otd["000:HB5"]  = OTData("000", "R", "HB5", "BF", 0, 1, "", "Master status: Summer/winter mode")
        self.otd["000:HB6"]  = OTData("000", "R", "HB6", "BF", 0, 1, "", "Master status: DHW blocking")
        self.otd["000:HB7"]  = OTData("000", "R", "HB7", "BF", 0, 1, "", "Master status: reserved")
        self.otd["000:LB0"]  = OTData("000", "R", "LB0", "BF", 0, 1, "", "Slave Status: Fault")
        self.otd["000:LB1"]  = OTData("000", "R", "LB1", "BF", 0, 1, "", "Slave Status: CH mode")
        self.otd["000:LB2"]  = OTData("000", "R", "LB2", "BF", 0, 1, "", "Slave Status: DHW mode")
        self.otd["000:LB3"]  = OTData("000", "R", "LB3", "BF", 0, 1, "", "Slave Status: Flame on")
        self.otd["000:LB4"]  = OTData("000", "R", "LB4", "BF", 0, 1, "", "Slave Status: Cooling on")
        self.otd["000:LB5"]  = OTData("000", "R", "LB5", "BF", 0, 1, "", "Slave Status: CH2 active")
        self.otd["000:LB6"]  = OTData("000", "R", "LB6", "BF", 0, 1, "", "Slave Status: Diagnostic/service indication")
        self.otd["000:LB7"]  = OTData("000", "R", "LB7", "BF", 0, 1, "", "Slave Status: Electricity production")
        self.otd["001"]      = OTData("001", "RW", "",    "", 0, 100, "°C", "CH water temperature Setpoint")
        self.otd["001W"]     = OTData("001", "W", "",    "F8.8", 0, 100, "°C", "CH water temperature Setpoint")
        self.otd["001R"]     = OTData("001", "R", "",    "F8.8", 0, 100, "°C", "CH water temperature Setpoint")
        self.otd["002"]      = OTData("002", "W", "",    "BF", 0, 1, "", "Master configuration")
        self.otd["002:LB"]   = OTData("002", "W", "0-7", "U8", 0, 255, "", "Master configuration: MemberId code")
        self.otd["002:HB0"]  = OTData("002", "W", "HB0", "BF", 0, 1, "", "Master configuration: Smart power")
        self.otd["003"]      = OTData("003", "R", "",    "BF", 0, 1, "", "Slave configuration")
        self.otd["003:LB"]   = OTData("003", "R", "0-7", "U8", 0, 255, "", "Slave configuration: MemberId code")
        self.otd["003:HB0"]  = OTData("003", "R", "HB0", "BF", 0, 1, "", "Slave configuration: DHW present")
        self.otd["003:HB1"]  = OTData("003", "R", "HB1", "BF", 0, 1, "", "Slave configuration: On/Off control only")
        self.otd["003:HB2"]  = OTData("003", "R", "HB2", "BF", 0, 1, "", "Slave configuration: Cooling supported")
        self.otd["003:HB3"]  = OTData("003", "R", "HB3", "BF", 0, 1, "", "Slave configuration: DHW configuration")
        self.otd["003:HB4"]  = OTData("003", "R", "HB4", "BF", 0, 1, "", "Slave configuration: Master low-off&pump control allowed")
        self.otd["003:HB5"]  = OTData("003", "R", "HB5", "BF", 0, 1, "", "Slave configuration: CH2 present")
        self.otd["003:HB6"]  = OTData("003", "R", "HB6", "BF", 0, 1, "", "Slave configuration: Remote water filling function")
        self.otd["003:HB7"]  = OTData("003", "R", "HB7", "BF", 0, 1, "", "Heat/cool mode control")
        self.otd["004"]      = OTData("004", "RW", "",   "",   0, 0, "", "Slave control")
        self.otd["004W"]     = OTData("004", "W", "8-15", "U8", 0, 255, "", "==1 Boiler Lockout-reset;==10 Service request reset;==2 Request Water filling")
        self.otd["004R"]     = OTData("004", "R", "0-7", "U8", 0, 255, "", ">127 response ok;<128 response error")
        self.otd["005"]      = OTData("005", "R", "",    "BF", 0, 1, "", "Boiler faults")
        self.otd["005:HB0"]  = OTData("005", "R", "HB0", "BF", 0, 1, "", "Service required")
        self.otd["005:HB1"]  = OTData("005", "R", "HB1", "BF", 0, 1, "", "Lockout-reset enabled")
        self.otd["005:HB2"]  = OTData("005", "R", "HB2", "BF", 0, 1, "", "Low water pressure")
        self.otd["005:HB3"]  = OTData("005", "R", "HB3", "BF", 0, 1, "", "Gas/flame fault")
        self.otd["005:HB4"]  = OTData("005", "R", "HB4", "BF", 0, 1, "", "Air pressure fault")
        self.otd["005:HB5"]  = OTData("005", "R", "HB5", "BF", 0, 1, "", "Water over-temperature")
        self.otd["005:LB"]   = OTData("005", "R", "0-7", "U8", 0, 255, "", "OEM fault code")
        self.otd["006"]      = OTData("006", "R", "",    "BF", 0, 1, "", "Remote boiler parameters")
        self.otd["006:HB0"]  = OTData("006", "R", "HB0", "BF", 0, 1, "", "transfer-enabled: DHW setpoint")
        self.otd["006:HB1"]  = OTData("006", "R", "HB1", "BF", 0, 1, "", "transfer-enabled: max. CH setpoint")
        self.otd["006:HB2"]  = OTData("006", "R", "HB2", "BF", 0, 1, "", "transfer-enabled: param 2 (OTC HC ratio)") # unsure
        self.otd["006:HB3"]  = OTData("006", "R", "HB3", "BF", 0, 1, "", "transfer-enabled: param 3")
        self.otd["006:HB4"]  = OTData("006", "R", "HB4", "BF", 0, 1, "", "transfer-enabled: param 4")
        self.otd["006:HB5"]  = OTData("006", "R", "HB5", "BF", 0, 1, "", "transfer-enabled: param 5")
        self.otd["006:HB6"]  = OTData("006", "R", "HB6", "BF", 0, 1, "", "transfer-enabled: param 6")
        self.otd["006:HB7"]  = OTData("006", "R", "HB7", "BF", 0, 1, "", "transfer-enabled: param 7")
        self.otd["006:LB0"]  = OTData("006", "R", "LB0", "BF", 0, 1, "", "read/write: DHW setpoint")
        self.otd["006:LB1"]  = OTData("006", "R", "LB1", "BF", 0, 1, "", "read/write: max. CH setpoint")
        self.otd["006:LB2"]  = OTData("006", "R", "LB2", "BF", 0, 1, "", "read/write: param 2 (OTC HC ratio)") # unsure
        self.otd["006:LB3"]  = OTData("006", "R", "LB3", "BF", 0, 1, "", "read/write: param 3")
        self.otd["006:LB4"]  = OTData("006", "R", "LB4", "BF", 0, 1, "", "read/write: param 4")
        self.otd["006:LB5"]  = OTData("006", "R", "LB5", "BF", 0, 1, "", "read/write: param 5")
        self.otd["006:LB6"]  = OTData("006", "R", "LB6", "BF", 0, 1, "", "read/write: param 6")
        self.otd["006:LB7"]  = OTData("006", "R", "LB7", "BF", 0, 1, "", "read/write: param 7")
        self.otd["007"]      = OTData("007", "W", "",    "F8.8", 0, 100, "%", "Cooling control signal")
        self.otd["008"]      = OTData("008", "W", "",    "F8.8", 0, 100, "°C", "Control Setpoint for 2nd CH circuit")
        self.otd["009"]      = OTData("009", "R", "",    "F8.8", 0, 30, "", "Remote override room Setpoint") # 0 - no override
        self.otd["010"]      = OTData("010", "R", "8-15", "U8", 0, 255, "", "Number of Transparent-Slave-Parameters supported by slave")
        self.otd["011"]      = OTData("011", "RW", "",   "",  0, 0,  "", "Index/Value of transparent slave parameter")
        self.otd["011R"]     = OTData("011", "R", "",    "BF", 0, 9, "", "Transparent slave parameter")
        self.otd["011R:HB"]  = OTData("011", "R", "8-15", "U8", 0, 255, "", "Index of read transparent slave parameter")
        self.otd["011R:LB"]  = OTData("011", "R", "0-7", "U8", 0, 255, "", "Value of read transparent slave parameter")
        self.otd["011W"]     = OTData("011", "W", "",    "BF", 0, 1, "", "Transparent slave parameter to write")
        self.otd["011W:HB"]  = OTData("011", "W", "8-15","U8", 0, 255, "", "Index of referred-to transparent slave parameter to write")
        self.otd["011W:LB"]  = OTData("011", "W", "0-7", "U8", 0, 255, "", "Value of referred-to transparent slave parameter to write")
        self.otd["012"]      = OTData("012", "R", "8-15", "U8"  , 0, 255, "", "Size of Fault-History-Buffer supported by slave")
        self.otd["013"]      = OTData("013", "R", ""   , "BF", 0, 1, "", "Fault-history buffer entry")
        self.otd["013:HB"]   = OTData("013", "R", "8-15", "U8", 0, 255, "", "Index number")
        self.otd["013:LB"]   = OTData("013", "R", "0-7", "U8", 0, 255, "", "Entry Value")
        self.otd["014"]      = OTData("014", "W", ""   , "F8.8", 0, 100, "", "Maximum relative modulation level setting (%)")
        self.otd["015"]      = OTData("015", "R", ""   , "BF", 0, 0, "", "Boiler capacities")
        self.otd["015:HB"]   = OTData("015", "R", "8-15", "U8" , 0, 255, "kW", "Maximum boiler capacity")
        self.otd["015:LB"]   = OTData("015", "R", "0-7", "U8", 0, 100, "%", "Minimum boiler modulation level")
        self.otd["016"]      = OTData("016", "W", ""   , "F8.8", -40, 127, "°C", "Room Setpoint")
        self.otd["017"]      = OTData("017", "R", ""   , "F8.8", 0, 100, "%", "Relative Modulation Level")
        self.otd["018"]      = OTData("018", "R", ""   , "F8.8", 0, 5, "bar", "Water pressure in CH circuit")
        self.otd["019"]      = OTData("019", "R", ""   , "F8.8", 0, 16, "l/min", "Water flow rate in DHW circuit")
        self.otd["020"]      = OTData("020", "RW", ""  , ""  , 0, 0, "", "Time and DoW")
        self.otd["020R"]     = OTData("020", "R", ""   , "BF"  , 0, 0, "", "")
        self.otd["020R:HB0"] = OTData("020", "R", "13-15", "U8", 0, 7, "", "Day of Week")
        self.otd["020R:HB1"] = OTData("020", "R", "8-12", "U8", 0, 23, "", "Hours")
        self.otd["020R:LB"]  = OTData("020", "R", "0-7", "U8", 0, 59, "", "Minutes")
        self.otd["020W"]     = OTData("020", "W", ""   , ""  , 0, 0, "", "Day of Week and Time of Day")
        self.otd["020W:HB0"] = OTData("020", "W", "13-15", "U8", 0, 7, "", "Day of Week")
        self.otd["020W:HB1"] = OTData("020", "W", "8-12", "U8", 0, 23, "", "Hours")
        self.otd["020W:LB"]  = OTData("020", "W", "0-7", "U8", 0, 59, "", "Minutes")
        self.otd["021"]      = OTData("021", "RW", ""   , ""  , 0, 0, "", "Calendar date")
        self.otd["021R"]     = OTData("021", "R", ""   , "BF", 0, 0, "", "Calendar date")
        self.otd["021R:HB"]  = OTData("021", "R", "8-15", "U8", 1, 12, "", "Month")
        self.otd["021R:LB"]  = OTData("021", "R", "0-7", "U8", 1, 31, "", "Day")
        self.otd["021W"]     = OTData("021", "W", ""   , "BF", 0, 0, "", "")
        self.otd["021W:HB"]  = OTData("021", "W", "8-15", "U8", 1, 12, "", "Month")
        self.otd["021W:LB"]  = OTData("021", "W", "0-7", "U8", 1, 31, "", "Day")
        self.otd["022"]      = OTData("022", "RW", ""  , ""  , 0, 0, "", "Calendar year")
        self.otd["022R"]     = OTData("022", "R", ""   , "U16", 0, 65535, "", "Year")
        self.otd["022W"]     = OTData("022", "W", ""   , "U16", 0, 65535, "", "Year")
        self.otd["023"]      = OTData("023", "W", ""   , "F8.8", -40, 127, "°C", "Room Setpoint for 2nd CH circuit")
        self.otd["024"]      = OTData("024", "W", ""   , "F8.8", -40, 127, "°C", "Room temperature (°C)")
        self.otd["025"]      = OTData("025", "R", ""   , "F8.8", -40, 127, "°C", "Boiler flow water temperature")
        self.otd["026"]      = OTData("026", "R", ""   , "F8.8", -40, 127, "°C", "DHW temperature")
        self.otd["027"]      = OTData("027", "R", ""   , "F8.8", -40, 127, "°C", "Outside temperature")
        self.otd["028"]      = OTData("028", "R", ""   , "F8.8", -40, 127, "°C", "Return water temperature")
        self.otd["029"]      = OTData("029", "R", ""   , "F8.8", -40, 127, "°C", "Solar storage temperature")
        self.otd["030"]      = OTData("030", "R", ""   , "S16", -40, 250, "°C", "Solar collector temperature")
        self.otd["031"]      = OTData("031", "R", ""   , "F8.8", -40, 127, "°C", "Flow water temperature CH2 circuit")
        self.otd["032"]      = OTData("032", "R", ""   , "F8.8", -40, 127, "°C", "Domestic hot water temperature 2")
        self.otd["033"]      = OTData("033", "R", ""   , "S16", -40, 500, "°C", "Boiler exhaust temperature")
        self.otd["034"]      = OTData("034", "R", ""   , "F8.8", -40, 127, "°C", "Boiler heat exchanger temperature") # unsure
        self.otd["035"]      = OTData("035", "R", ""   , "U16"  , 0, 0, "", "Boiler fan speed setpoint") # unsure
# could also be
#        self.otd["035:HB"]      = OTData("035", "R", ""   , "U8"  , 0, 255, "", "Boiler fan speed Setpoint")
#        self.otd["035:LB"]      = OTData("035", "R", ""   , "U8"  , 0, 255, "", "Boiler fan speed actual value")
        self.otd["036"]      = OTData("036", "R", ""   , "F8.8"  , -128, 127, "µA", "Electrical current through burner flame") # unsure
        self.otd["037"]      = OTData("037", "W", ""   , "F8.8"  , -40, 127, "°C", "Room temperature for 2nd CH circuit") # unsure
        self.otd["038"]      = OTData("038", "W", ""   , "F8.8"  , 0, 0, "%", "Relative Humidity") # unsure
        self.otd["048"]      = OTData("048", "R", ""   , "BF"  , 0, 0, "", "DHW Setpoint bounds for adjustment")
        self.otd["048:HB"]   = OTData("048", "R", "8-15", "S8" , 0, 127, "°C", "Upper bound")
        self.otd["048:LB"]   = OTData("048", "R", "0-7", "S8"  , 0, 127, "°C", "Lower bound")
        self.otd["049"]      = OTData("049", "R", ""   , "BF"  , 0, 0, "°C", "Max CH water Setpoint bounds for adjustment")
        self.otd["049:HB"]   = OTData("049", "R", "8-15", "S8" , 0, 127, "°C", "Upper bound")
        self.otd["049:LB"]   = OTData("049", "R", "0-7", "S8"  , 0, 127, "°C", "Lower bound")
        self.otd["050"]      = OTData("050", "R", ""   , "BF"  , 0, 0, "", "OTC HC-Ratio bounds") # unsure
        self.otd["050:HB"]   = OTData("050", "R", "8-15", "S8" , -128, 127, "", "Upper bound")
        self.otd["050:LB"]   = OTData("050", "R", "0-7", "S8"  , -128, 127, "", "Lower bound")
        self.otd["051"]      = OTData("051", "R", ""   , "BF"  , 0, 0, "", "Remote param 3") # unsure
        self.otd["051:HB"]   = OTData("051", "R", "8-15", "S8" , -128, 127, "", "Upper bound")
        self.otd["051:LB"]   = OTData("051", "R", "0-7", "S8"  , -128, 127, "", "Lower bound")
        self.otd["052"]      = OTData("052", "R", ""   , "BF"  , 0, 0, "", "Remote param 4") # unsure
        self.otd["052:HB"]   = OTData("052", "R", "8-15", "S8" , -128, 127, "", "Upper bound")
        self.otd["052:LB"]   = OTData("052", "R", "0-7", "S8"  , -128, 127, "", "Lower bound")
        self.otd["053"]      = OTData("053", "R", ""   , "BF"  , 0, 0, "", "Remote param 5") # unsure
        self.otd["053:HB"]   = OTData("053", "R", "8-15", "S8" , -128, 127, "", "Upper bound")
        self.otd["053:LB"]   = OTData("053", "R", "0-7", "S8"  , -128, 127, "", "Lower bound")
        self.otd["054"]      = OTData("054", "R", ""   , "BF"  , 0, 0, "", "Remote param 6") # unsure
        self.otd["054:HB"]   = OTData("054", "R", "8-15", "S8" , -128, 127, "", "Upper bound")
        self.otd["054:LB"]   = OTData("054", "R", "0-7", "S8"  , -128, 127, "", "Lower bound")
        self.otd["055"]      = OTData("055", "R", ""   , "BF"  , 0, 0, "", "Remote param 7") # unsure
        self.otd["055:HB"]   = OTData("055", "R", "8-15", "S8" , -128, 127, "", "Upper bound")
        self.otd["055:LB"]   = OTData("055", "R", "0-7", "S8"  , -128, 127, "", "Lower bound")
        self.otd["056"]      = OTData("056", "RW", ""  , ""  , 0, 0, "°C", "DHW Setpoint (Remote param 0)")
        self.otd["056R"]     = OTData("056", "R", ""   , "F8.8", 0, 127, "°C", "Current DHW Setpoint (Remote param 0)")
        self.otd["056W"]     = OTData("056", "W", ""   , "F8.8", 0, 127, "°C", "DHW Setpoint to set(Remote param 0)")
        self.otd["057"]      = OTData("057", "RW", ""  , ""  , 0, 0, "°C", "Max CH water Setpoint (Remote param 1)")
        self.otd["057R"]     = OTData("057", "R", ""   , "F8.8", 0, 127, "°C", "Current Max CH water Setpoint (Remote param 1)")
        self.otd["057W"]     = OTData("057", "W", ""   , "F8.8", 0, 127, "°C", "Max CH water Setpoint to set (Remote param 1)")
        self.otd["058"]      = OTData("058", "RW", ""  , ""  , 0, 0, "°C", "OTC HC Ratio (Remote param 2)") # unsure
        self.otd["058R"]     = OTData("058", "R", ""   , "F8.8", 0, 127, "°C", "Current OTC HC Ratio (Remote param 2)") # unsure
        self.otd["058W"]     = OTData("058", "W", ""   , "F8.8", 0, 127, "°C", "OTC HC Ratio to set (Remote param 2)") # unsure
        self.otd["059"]      = OTData("059", "RW", ""  , ""  , 0, 0, "", "(Remote param 3)")
        self.otd["059R"]     = OTData("059", "R", ""   , "F8.8", 0, 127, "", "Current (Remote param 3)")
        self.otd["059W"]     = OTData("059", "W", ""   , "F8.8", 0, 127, "", "to set (Remote param 3)")
        self.otd["060"]      = OTData("060", "RW", ""  , ""  , 0, 0, "", "(Remote param 4)")
        self.otd["060R"]     = OTData("060", "R", ""   , "F8.8", 0, 127, "", "Current (Remote param 4)")
        self.otd["060W"]     = OTData("060", "W", ""   , "F8.8", 0, 127, "", "to set (Remote param 4)")
        self.otd["061"]      = OTData("061", "RW", ""  , ""  , 0, 0, "", "(Remote param 5)")
        self.otd["061R"]     = OTData("061", "R", ""   , "F8.8", 0, 127, "", "Current (Remote param 5)")
        self.otd["061W"]     = OTData("061", "W", ""   , "F8.8", 0, 127, "", "to set (Remote param 5)")
        self.otd["062"]      = OTData("062", "RW", ""  , ""  , 0, 0, "", "(Remote param 6)")
        self.otd["062R"]     = OTData("062", "R", ""   , "F8.8", 0, 127, "", "Current (Remote param 6)")
        self.otd["062W"]     = OTData("062", "W", ""   , "F8.8", 0, 127, "", "to set (Remote param 6)")
        self.otd["063"]      = OTData("063", "RW", ""  , ""  , 0, 0, "", "(Remote param 7)")
        self.otd["063R"]     = OTData("063", "R", ""   , "F8.8", 0, 127, "", "Current (Remote param 7)")
        self.otd["063W"]     = OTData("063", "W", ""   , "F8.8", 0, 127, "", "to set (Remote param 7)")
        self.otd["070"]      = OTData("070", "R", "",    "BF", 0, 0, "", "Status ventilation / heat-recovery") # unsure
        self.otd["070:HB0"]  = OTData("070", "R", "HB0", "BF", 0, 1, "", "Master status ventilation / heat-recovery: Ventilation enable") # unsure
        self.otd["070:HB1"]  = OTData("070", "R", "HB1", "BF", 0, 1, "", "Master status ventilation / heat-recovery: Bypass postion") # unsure
        self.otd["070:HB2"]  = OTData("070", "R", "HB2", "BF", 0, 1, "", "Master status ventilation / heat-recovery: Bypass mode") # unsure
        self.otd["070:HB3"]  = OTData("070", "R", "HB3", "BF", 0, 1, "", "Master status ventilation / heat-recovery: Free ventilation mode") # unsure
        self.otd["070:LB0"]  = OTData("070", "R", "LB0", "BF", 0, 1, "", "Slave status ventilation / heat-recovery: Fault indication") # unsure
        self.otd["070:LB1"]  = OTData("070", "R", "LB1", "BF", 0, 1, "", "Slave status ventilation / heat-recovery: Ventilation mode") # unsure
        self.otd["070:LB2"]  = OTData("070", "R", "LB2", "BF", 0, 1, "", "Slave status ventilation / heat-recovery: Bypass status") # unsure
        self.otd["070:LB3"]  = OTData("070", "R", "LB3", "BF", 0, 1, "", "Slave status ventilation / heat-recovery: Bypass automatic status") # unsure
        self.otd["070:LB4"]  = OTData("070", "R", "LB4", "BF", 0, 1, "", "Slave status ventilation / heat-recovery: Free ventilation status") # unsure
        self.otd["070:LB6"]  = OTData("070", "R", "LB6", "BF", 0, 1, "", "Slave status ventilation / heat-recovery: Diagnostic indication") # unsure
        self.otd["071"]      = OTData("071", "R", ""   , ""  , 0, 0, "", "Relative ventilation position (0-100%). 0% is the minimum set ventilation and 100% is the maximum set ventilation") # unsure
        self.otd["072"]      = OTData("072", "R", ""   , ""  , 0, 0, "", "Application-specific fault flags and OEM fault code ventilation / heat-recovery") # unsure
        self.otd["073"]      = OTData("073", "R", ""   , ""  , 0, 0, "", "An OEM-specific diagnostic/service code for ventilation / heat-recovery system") # unsure
        self.otd["074"]      = OTData("074", "R", "",    "BF", 0, 1, "", "Slave Configuration ventilation / heat-recovery") # unsure
        self.otd["074:HB0"]  = OTData("074", "R", "HB0", "BF", 0, 1, "", "Ventilation enabled") # unsure
        self.otd["074:HB1"]  = OTData("074", "R", "HB1", "BF", 0, 1, "", "Bypass position") # unsure
        self.otd["074:HB2"]  = OTData("074", "R", "HB2", "BF", 0, 1, "", "Bypass mode") # unsure
        self.otd["074:HB3"]  = OTData("074", "R", "HB3", "BF", 0, 1, "", "Speed control") # unsure
        self.otd["074:LB"]   = OTData("074", "R", "0-7", "U8", 0, 255, "", "Slave MemberID Code ventilation / heat-recovery") # unsure
#!!! not properly checked from below
        self.otd["075"]      = OTData("075", "R", ""   , "U16", 0, 0, "", "The implemented version of the OpenTherm Protocol Specification in the ventilation / heat-recovery system")
        self.otd["076"]      = OTData("076", "R", ""   , "U16", 0, 0, "", "Ventilation / heat-recovery product version number and type")
        self.otd["077"]      = OTData("077", "R", ""   , "U16", 0, 100, "%", "Relative ventilation")
        self.otd["078"]      = OTData("078", "R", ""   , "U16", 0, 100, "%", "Relative humidity exhaust air")
        self.otd["079"]      = OTData("079", "R", ""   , "U16", 0, 2000, "ppm", "CO2 level exhaust air")
        self.otd["080"]      = OTData("080", "R", ""   , "U16", 0, 0, "°C", "Supply inlet temperature")
        self.otd["081"]      = OTData("081", "R", ""   , "U16", 0, 0, "°C", "Supply outlet temperature")
        self.otd["082"]      = OTData("082", "R", ""   , "U16", 0, 0, "°C", "mExhaust inlet temperature")
        self.otd["083"]      = OTData("083", "R", ""   , "U16", 0, 0, "°C", "Exhaust outlet temperature")
        self.otd["084"]      = OTData("084", "R", ""   , "U16", 0, 0, "rpm", "Exhaust fan speed")
        self.otd["085"]      = OTData("085", "R", ""   , "U16", 0, 0, "rpm", "Supply fan speed")
        self.otd["086"]      = OTData("086", "R", "",    "BF", 0, 0, "", "Remote ventilation / heat-recovery parameter:")
        self.otd["086:HB0"]  = OTData("086", "R", "HB0", "BF", 0, 0, "", "Transfer-enable: Nominal ventilation value")
        self.otd["086:LB0"]  = OTData("086", "R", "LB0", "BF", 0, 0, "", "Read/write : Nominal ventilation value")
        self.otd["087"]      = OTData("087", "R", ""   , "U16", 0, 100, "%", "Nominal relative value for ventilation")
        self.otd["088"]      = OTData("088", "R", ""   , "U16", 0, 255, "", "Number of Transparent-Slave-Parameters supported by TSP’s ventilation / heat-recovery")
        self.otd["089"]      = OTData("089", "R", ""   , "U16", 0, 255, "", "Index number / Value of referred-to transparent TSP’s ventilation / heat-recovery parameter")
        self.otd["090"]      = OTData("090", "R", ""   , "U16", 0, 255, "", "Size of Fault-History-Buffer supported by ventilation / heat-recovery")
        self.otd["091"]      = OTData("091", "R", ""   , "U16", 0, 255, "", "Index number / Value of referred-to fault-history buffer entry ventilation / heat-recovery")
# from https://www.opentherm.eu/request-details/?post_ids=3931
        self.otd["093"]      = OTData("093", "R", ""   , "U16", 0, 65535, "", "Brand Index / Slave Brand name")
        self.otd["094"]      = OTData("094", "R", ""   , "U16", 0, 65535, "", "Brand Version Index / Slave product type/version")
        self.otd["095"]      = OTData("095", "R", ""   , "U16", 0, 65535, "", "Brand Serial Number index / Slave product serialnumber")
        self.otd["098"]      = OTData("098", "R", ""   , "U16", 0, 255, "", "For a specific RF sensor the RF strength and battery level is written")
        self.otd["099"]      = OTData("099", "R", ""   , "U16", 0, 255, "", "Operating Mode HC1, HC2/ Operating Mode DHW")
# to check
        self.otd["100"]      = OTData("100", "R", ""   , "U16", 0, 255, "", "Function of manual and program changes in master and remote room Setpoint")
        self.otd["101"]      = OTData("101", "R", ""   , "BF", 0, 0, "", "Solar Storage:")
        self.otd["101:HB"]   = OTData("101", "R", "8-10", "U8", 0, 0, "", "Master Solar Storage: Solar mode")
        self.otd["101:LB0"]  = OTData("101", "R", "LB0", "BF", 0, 0, "", "Slave Solar Storage: Fault indication")
        self.otd["101:LB1"]  = OTData("101", "R", "1-3", "U8", 0, 7, "", "Slave Solar Storage: Solar mode status")
        self.otd["101:LB2"]  = OTData("101", "R", "4-5", "U8", 0, 3, "", "Slave Solar Storage: Solar status")
        self.otd["102"]      = OTData("102", "R", ""   , ""  , 0, 0, "", "Application-specific fault flags and OEM fault code Solar Storage")
        self.otd["103"]      = OTData("103", "R", "",    "BF", 0, 0, "", "Slave Configuration Solar Storage")
        self.otd["103:HB0"]  = OTData("103", "R", "HB0", "BF", 0, 0, "", "System type")
        self.otd["103:LB"]   = OTData("103", "R", "0-7", "U8", 0, 255, "", "Slave MemberID")
        self.otd["104"]      = OTData("104", "R", ""   , "U16", 0, 255, "", "Solar Storage product version number and type")
        self.otd["105"]      = OTData("105", "R", ""   , "U16", 0, 255, "", "Number of Transparent-Slave-Parameters supported by TSP’s Solar Storage")
        self.otd["106"]      = OTData("106", "R", ""   , "U16", 0, 255, "", "Index number / Value of referred-to transparent TSP’s Solar Storage parameter")
        self.otd["107"]      = OTData("107", "R", ""   , "U16", 0, 255, "", "Size of Fault-History-Buffer supported by Solar Storage")
        self.otd["108"]      = OTData("108", "R", ""   , "U16", 0, 255, "", "Index number / Value of referred-to fault-history buffer entry Solar Stor")
        self.otd["109"]      = OTData("109", "R", ""   , "U16", 0, 255, "", "Electricity producer starts")
        self.otd["110"]      = OTData("110", "R", ""   , "U16", 0, 255, "", "Electricity producer hours")
        self.otd["111"]      = OTData("111", "R", ""   , "U16", 0, 255, "", "Electricity production")
        self.otd["112"]      = OTData("112", "R", ""   , "U16", 0, 255, "", "Cumulativ Electricity production")
        self.otd["113"]      = OTData("113", "R", ""   , "U16", 0, 255, "", "Number of un-successful burner starts")
        self.otd["114"]      = OTData("114", "R", ""   , "U16", 0, 255, "", "Number of times flame signal was too low")
# below data-ids are checked up against specs
        self.otd["115"]      = OTData("115", "R", ""   , "U16", 0, 255, "", "OEM-specific diagnostic/service code")
        # below ids are RW (write 0 to reset)
        self.otd["116"]      = OTData("116", "R", ""   , "U16", 0, 65535, "", "Number of succesful starts burner")
        self.otd["117"]      = OTData("117", "R", ""   , "U16", 0, 65535, "", "Number of starts CH pump")
        self.otd["118"]      = OTData("118", "R", ""   , "U16", 0, 65535, "", "Number of starts DHW pump/valve")
        self.otd["119"]      = OTData("119", "R", ""   , "U16", 0, 65535, "", "Number of starts burner during DHW mode")
        self.otd["120"]      = OTData("120", "R", ""   , "U16", 0, 65535, "", "Number of hours that burner is in operation (i.e. flame on)")
        self.otd["121"]      = OTData("121", "R", ""   , "U16", 0, 65535, "", "Number of hours that CH pump has been running")
        self.otd["122"]      = OTData("122", "R", ""   , "U16", 0, 65535, "", "Number of hours that DHW pump has been running or DHW valve has been opened")
        self.otd["123"]      = OTData("123", "R", ""   , "U16", 0, 65535, "", "Number of hours that burner is in operation during DHW mode")
        # ^^^
        self.otd["124"]      = OTData("124", "W", ""   , "F8.8", 1, 127, "", "The implemented version of the OpenTherm Protocol Specification in the master")
        self.otd["125"]      = OTData("125", "R", ""   , "F8.8", 1, 127, "", "The implemented version of the OpenTherm Protocol Specification in the slave")
        self.otd["126"]      = OTData("126", "W", ""   , "BF"  , 0, 0, "", "Master product version number and type")
        self.otd["126:HB"]   = OTData("126", "W", "8-15", "U8"  , 0, 255, "", "Master product version number and type")
        self.otd["126:LB"]   = OTData("126", "W", "0-7", "U8"  , 0, 255, "", "Master product version number and type")
        self.otd["127"]      = OTData("127", "R", ""   , "BF"  , 0, 0, "", "Slave product version number and type")
        self.otd["127:HB"]   = OTData("127", "R", "8-15", "U8"  , 0, 255, "", "Slave product version number and type")
        self.otd["127:LB"]   = OTData("127", "R", "0-7", "U8"  , 0, 255, "", "Slave product version number and type")
# baxi ecofour unspecified regs
        self.otd["129"]      = OTData("129", "R", ""   , "U16", 0, 65535, "", "BAXI data-id 129")
        self.otd["130"]      = OTData("130", "R", ""   , "U16", 0, 65535, "", "BAXI data-id 130")
        self.otd["149"]      = OTData("149", "R", ""   , "U16", 0, 65535, "", "BAXI data-id 149")
        self.otd["150"]      = OTData("150", "R", ""   , "U16", 0, 65535, "", "BAXI data-id 150")
        self.otd["151"]      = OTData("151", "R", ""   , "U16", 0, 65535, "", "BAXI data-id 151")
        self.otd["173"]      = OTData("173", "R", ""   , "U16", 0, 65535, "", "BAXI data-id 173")
        self.otd["198"]      = OTData("198", "R", ""   , "U16", 0, 65535, "", "BAXI data-id 198")
        self.otd["199"]      = OTData("199", "R", ""   , "U16", 0, 65535, "", "BAXI data-id 199")
        self.otd["200"]      = OTData("200", "R", ""   , "U16", 0, 65535, "", "BAXI data-id 200")
        self.otd["202"]      = OTData("202", "R", ""   , "U16", 0, 65535, "", "BAXI data-id 202")
        self.otd["203"]      = OTData("203", "R", ""   , "U16", 0, 65535, "", "BAXI data-id 203")
        self.otd["204"]      = OTData("204", "R", ""   , "U16", 0, 65535, "", "BAXI data-id 204")
        self.otd["209"]      = OTData("209", "R", ""   , "U16", 0, 65535, "", "BAXI data-id 209")


        # from https://otgw.tclcode.com/details.cgi
        self.OT_MEMBERS = { 
            0: "Unspecified",
            2: "AWB",
            4: "Multibrand", # Atag, Baxi Slim, Brötje, Elco
            5: "Itho Daalderop",
            6: "Daikin/Ideal",
            8: "Biasi/Buderus/Logamax", 
            9: "Ferroli/Agpo",
            11: "De Dietrich/Remeha/Baxi Prime", 
            13: "Cetetherm",
            16: "Unical",
            18: "Bosch",
            24: "Vaillant/AWB/Bulex", 
            27: "Baxi",
            29: "Daalderop/Itho",
            33: "Viessmann",
            41: "Radiant",
            56: "Baxi Luna",
            131: "Netfit/Bosch",
            173: "Intergas"
        }


    # return opentherm master-to-slave and slave-to-master message names by numeric id
    def msg_descr(self, resp):
        if resp < 0 or resp > 7:
            return "!LAME!"
        else:
            return self.OT_MSGS[resp]

    # extract response bits based on otd descriptions of data position
    def get_bits(self, value, pos):
        if pos[0:2] == "LB":
            return (value >> int(pos[2])) & 1
        elif pos[0:2] == "HB":
            return (value >> (8+int(pos[2]))) & 1
        else:
            iv = pos.split("-")
            mask = 0
            for i in range(0, int(iv[1]) - int(iv[0]) + 1):
                mask = (mask << 1) | 1
            return (value >> int(iv[0])) & mask
    
    # decode opentherm data-value based on it's pos and fmt; 
    # return: ( string representation, 1 if success else -1 )
    def decode_value(self, value, fmt, pos):
        if fmt == "U8" or fmt == "BF":
            if pos != "":
                v = self.get_bits(value, pos) & 0xff
            else:
                v  = (value & 0xff)
            return "%u" % v, 1
        elif fmt == "U16":
            return "%u" % (value & 0xffff), 1
        elif fmt == "S8":
            if pos != "":
                v = self.get_bits(value, pos) & 0xff
            else:
                v = value & 0xff
            if v > 127:
                return "-%u" % (256 - v), 1
            else:
                return "%u" % v, 1
        elif fmt == "S16":
            if value > 32767:
                return "-%u" % (65536 - (value & 0xffff)), 1
            else:
                return "%u" % (value & 0xffff), 1
        elif fmt == "F8.8": # # 1/256 gives 0.00390625 i.e. 8 fractional decimal digits but it useless in this certain practical case, let's limit output to 3 decimal digits to get something out of smallest possible number
            v = value & 0xffff
            if v > 32767:
                return ("-%.3f" % ((65536-v)/256)).rstrip('0').rstrip('.'), 1 # default %f formatting produces extra trailing zeroes
            else:
                return ("%.3f" % (v/256)).rstrip('0').rstrip('.'), 1
        else:
            # unknown format
            return "###", -1

    # decode description string (if it has conditional parts separated by ';')
    def decode_descr(self, descr, val):
        if ';' not in descr:
            return descr;
        ds = descr.split(';')
        for d in ds:
            c  = d.split(' ', 1)
            if eval(val + c[0]):
                return c[1]
        return "unknown value " + val

    # decode and describe opentherm data-value using direct otd[] lookup by dids
    def describe_dataid(self, dids, val):
        descr = ""
        if self.otd[dids].fmt == "BF":
            descr = self.otd[dids].descr
            for variant in ( "HB", "HB0", "HB1", "HB2", "HB3", "HB4", "HB5", "HB6", "HB7", "LB", "LB0", "LB1", "LB2", "LB3", "LB4", "LB5", "LB6", "LB7" ):
                varid = dids + ":" + variant
                if varid in self.otd:
                    vv, vc = self.decode_value(val, self.otd[varid].fmt, self.otd[varid].pos)
                    if vc > 0:
                        if '-' in self.otd[varid].pos: # multibit field
                            descr = descr + "\n " + self.decode_descr(self.otd[varid].descr, vv) + " = " + vv + self.otd[varid].units
                            if float(vv) < self.otd[varid].min or float(vv) > self.otd[varid].max:
                                descr = descr + " - out of range!"
                        else:
                            descr = descr + "\n " + ("+" if vv == "1" else "-") + self.otd[varid].descr
                        if varid == "003:LB" or varid == "002:LB" or varid == "074:LB" or varid == "103:LB":
                            descr = descr + " (" + self.describe_member(int(vv)) + ")"
                    else:
                        return "Unable to decode value \'" + val + "\' as per fmt " + self.otd[varid].fmt + " from pos " +  self.otd[varid].pos , -1
        else:
            v,c = self.decode_value(val, self.otd[dids].fmt, self.otd[dids].pos)
            if c < 0:
                return "Unable to decode value \'" + val + "\' as per fmt " + self.otd[dids].fmt + " from pos " +  self.otd[dids].pos , -1
            descr = self.decode_descr(self.otd[dids].descr, v) + "\n " + v + self.otd[dids].units
            if float(v) < self.otd[dids].min or float(v) > self.otd[dids].max:
                descr = descr + " - out of range!"
        return descr, 1


    # describe either read or write opentherm request/response
    def describe_param_internal(self, dids, t_dir, val_sent, val_received, do_print):
        descr = ""
        if not dids in self.otd:
            if do_print:
                print("Data-id " + dids + " is unknown")
            return "Unknown data-id", -3
        if not t_dir in self.otd[dids].t_dir:
            if do_print:
                print("Data-id " + dids + "/" + t_dir + " is unknown")
            return "Unknown data-id direction", -3

        if t_dir == "R":
            if "I" in self.otd[dids].t_dir:
                descr = descr + "Read input value:\n"
                vv2, vc2 = self.describe_dataid(dids + "I", val_sent)
                if vc2 < 0:
                    return vv2, vc2
                descr = descr + vv2 + "\n"
            vv, vc = self.describe_dataid(dids, val_received)
            if vc < 0:
                return vv, vc
            descr = descr + "Response:\n" + vv
        else: # assume "W"
           vv, vc = self.describe_dataid(dids, val_sent)
           if vc < 0:
                return vv, vc
           descr = descr + "Written:\n" + vv + "\n"
           if "O" in self.otd[dids].t_dir:
                descr = descr + "Write output value:\n"
                vv2, vc2 = self.describe_dataid(dids + "I", val_received)
                if vc2 < 0:
                    return vv2, vc2
                descr = descr + vv2
        if do_print:
            print(descr)
        return descr, 1

    # most generic opentherm request/response decoder
    def describe_param(self, data_id, t_dir, value_sent, value_received, do_print=False):
        if value_sent is int:
            vsn = value_sent
        else:
            try:
                vsn = int(value_sent)
            except:
                if do_print:
                    print("Value \'" + value_sent + "\' is not a number")
                return "NaN", -2
        if value_received is int:
            vrn = value_received
        else:
            try:
                vrn = int(value_received)
            except:
                if do_print:
                    print("Value \'" + value_received + "\' is not a number")
                return "NaN", -2
        if type(data_id) is int:
            dids = "%03d" % data_id
        if type(data_id) is str:
            dids = ("00" + data_id)[-3:]
        if not dids in self.otd:
            if do_print:
                print("Data-id " + dids + " is unknown")
            return "Unknown data-id", -3
        if self.otd[dids].t_dir == "RW":
            r, c = self.describe_param_internal(dids + t_dir, t_dir, vsn, vrn, do_print)
            return self.otd[dids].descr + ": " + r, c
        return self.describe_param_internal(dids, t_dir, vsn, vrn, do_print)

    def describe_member(self, member_id):
        if member_id in self.OT_MEMBERS:
            return self.OT_MEMBERS[member_id]
        else:
            return "UNKNOWN"
         
    def parse_val(self, val):
        vn = ""
        if "+" in val:
            vals = val.split('+')
            sum = 0
            for v in vals:
                sum = sum + self.parse_val(v)
            return sum & 0xffff
        cv = "?" # for exception reporting
        try:
            if "%" in val:
                v = val.split('%')
                vn = v[0]
                if v[1] == "F8.8": # float %8.8f
                    cv = v[0]
                    nf = float(cv)
                    nf = nf * 256
                    n = int(nf)
                    return n & 0xffff
                elif v[1][0:1] == "B": # 16bit word's bit %B<N> or bitrange %B<N>-<M>
                    cv = vn
                    n = int(cv)
                    if "-" in v[1]:
                        br = v[1][1:].split('-')
                        vn = br[0]
                        cv = vn
                        bl = int(cv)
                        vn = br[1]
                        cv = vn
                        bh = int(cv)
                        mask = 0
                        for i in range(0, (bh - bl) + 1):
                            mask = (mask << 1) | 1
                        return (n & mask) << bl
                    else:
                        n = n & 1
                        vn = v[1][1:]
                        cv = vn
                        b = int(cv)
                        return n << b
                elif v[1][0:2] == "HB": # 8bit high byte or numbered bit %HB<N>
                    if v[1][2:3].isdigit:
                        cv = v[1][2:3]
                        bn = int(cv)
                        cv = vn
                        return (int(cv) & 255) << (8 + bn)
                    cv = vn
                    return (int(cv) & 255) << 8
                elif v[1][0:2] == "LB": # 8bit low byte or numbered bit %LB<N>
                    if v[1][2:3].isdigit:
                        cv = v[1][2:3]
                        bn = int(cv)
                        cv = vn
                        return (int(cv) & 255) << (bn)
                    cv = vn
                    return (int(cv) & 255)
                else:
                    raise ValueError("Invalid opentherm number format \'" + v[1] + "\'")
            cv = val # pure number
            n = int(cv)
            return n
        except:
            logging.error("Invalid numeric value \'" + cv + "\'")
            raise


##############
#
# Nevoton's module interaction generic interface
#
class Reply:
    def __init__(self, arrivalTime, replyTopic, replyData):
        self.arrivalTime = arrivalTime
        self.replyTopic = replyTopic
        self.replyData = replyData

class OpenthermInterface(ABC):

    @abstractmethod
    def get_device_id(self):
    # Return specific interface device id
        pass

    @abstractmethod
    def connect(self):
    # Connect to the nevoton's opentherm interface device
        pass

    @abstractmethod
    def connected(self):
    # report connected state
        pass

    @abstractmethod
    def send_cmd(self, cmd, cmdid, prm):
    #
    # Send transparent control command to Nevoton's opentherm gateway module
    # Numeric params:
    #   cmd - NCMD_READ or NCMD_WRITE
    #   cmdid - opentherm data-id to read/write
    #   prm - opentherm data-value to send along with data-it
    # Returns array of up to 4 elements
    #   [0] - numeric result; 1 for success (got READ-ACK or WRITE-ACK in response), negative values for errors
    #           tentative errors: -1 opentherm slave errors; -2 validation/communication errors; -3 protocol logical errors; -5 timeout; -7 nevoton gw comm error; -9 interface error; -100 internal error
    #               <-5 deemed to be fatal
    #   [1] - text of result/error description
    #   [2] - if result == 1 or -1 then opentherm response
    #   [3] - if result == 1 or -1 then opentherm response data
        pass

    def send_cmd_verbose(self, cmd, cmdid, prm):
        r = self.send_cmd(cmd, cmdid, prm)
        if (r[0] == 1 or r[0] == -1):
            resp = " with resp %u/0x%04x and resp data %u/0x%04x" % (r[2], r[2] & 0xffff, r[3], r[3] & 0xffff)
        else:
            resp = ""
        logging.debug("send_cmd() returning %d results: %d, \'%s\'%s" % (len(r), r[0], r[1], resp));
        return r

    @abstractmethod
    def disconnect(self):
    # Disconnect to the nevoton's opentherm interface device
        pass



##############
#
# Interact with Nevoton's module through the MQTT 'transparent exchange' topics
#
class OTMQTTInterfaсe(OpenthermInterface):

    def __init__(self, host, port, username, password, dev_id, verbose, ot_decoder):

        client_id = str(time.time()) + str(random.randint(0, 100000))

        self.client = mqtt.Client(client_id)
        self.client.on_log = self.process_log
        self.client.enable_logger(logging.getLogger(__name__))

        self.replyQ = queue.Queue()
        self.otdevice = dev_id
        self.verbose = verbose
        self.ot_decoder = ot_decoder
        self.client.on_message = self.process_mqtt_message
        self.dev_path = DEV_PREF + '/' + self.otdevice

        if username:
            self.client.username_pw_set(username, password)
        self.host = host
        self.port = port
        self.connQ = queue.Queue() # mqtt client connect() is not properly syncronized - use external queue-based sync

        self.mqtt_connected = False
        self.connected = False
        self.tr_cmd = ""
        self.tr_id = ""
        self.tr_data = ""
        logging.debug("OTMQTTInterfaсe initialized")

    def get_device_id(self):
    	return self.otdevice

    def save_msgdata(self, r):
        replyStr = str(r.replyData.decode("utf-8"))
        if NCTL_COMMAND in r.replyTopic:
            self.tr_cmd = replyStr
            logging.debug("Saving TR Command \'%s\'" % replyStr)
        elif NCTL_ID in r.replyTopic:
            self.tr_id = replyStr
            logging.debug("Saving TR ID \'%s\'" % replyStr)
        elif NCTL_DATA in r.replyTopic:
            self.tr_data = replyStr
            logging.debug("Saving TR Data \'%s\'" % replyStr)
        else:
            logging.warning("Got msg for unknown topic \'%s\' (%s)" % (r.replyTopic, replyStr))


    def clear_input(self, timeout, report_warnings):
        logging.info("clearing async mqtt data queue")
        dropped_items = 0
        qs = self.replyQ.qsize()
        logging.debug("queue size %d on clear_input(%d,%d)" % (qs, timeout, report_warnings))
        if report_warnings and (qs != 0):
            logging.warning("reply queue is not empty (%d)" % qs);
        if timeout == 0:
            try:
                while (True):
                    r = self.replyQ.get_nowait()
                    self.save_msgdata(r)
                    dropped_items += 1
                    if report_warnings:
                        logging.warning("unexpected queued item %d: t=%s, topic=%s, d=%s" % (dropped_items, time.strftime("%H:%M:%S", time.localtime(r.arrivalTime)), r.replyTopic, r.replyData ))
            except queue.Empty as err:
                pass
            logging.debug("clear_input/0 finished (%d items dropped)" % dropped_items)
            return dropped_items
        ts = time.perf_counter()
        remaining_timeout = timeout
        while (True):
            try:
                logging.debug("starting get(%f)" % remaining_timeout)
                r = self.replyQ.get(True, remaining_timeout)
                self.save_msgdata(r)
                dropped_items += 1
                logging.debug("dropping queued item %d: t=%s, topic=%s, d=%s" % (dropped_items, time.strftime("%H:%M:%S", time.localtime(r.arrivalTime)), r.replyTopic, r.replyData ))
            except queue.Empty as err:
                logging.debug("empty exception");
                pass
            remaining_timeout = timeout - (time.perf_counter() - ts)
            logging.debug("new remaining timeout %f" % remaining_timeout)
            if remaining_timeout <= 0:
                break
        logging.debug("clear_input finished (%d items dropped)" % dropped_items)
        return dropped_items


    def connect(self):
        logging.debug("connecting mqtt at %s:%d..." % (self.host, self.port))
        self.client.loop_start()
        self.client.on_connect = self.process_connect
        self.client.on_disconnect = self.process_disconnect

        ts = time.perf_counter()
        remaining_timeout = MQTT_CONN_TIMEOUT
        try:
            r = self.client.connect(self.host, self.port, MQTT_CONN_TIMEOUT, bind_address="")
        except Exception as e:
            ex = "Got mqtt connection exception: %s" % e
            logging.error(ex)
            return -7, ex

        logging.debug("mqtt connect() returned %d", r)
        while (True):
            try:
                r = self.connQ.get(True, remaining_timeout)
                logging.debug("got connect() notification (connected=%d)" % self.connected)
                if self.mqtt_connected:
                    break;
            except queue.Empty as err:
                pass
            remaining_timeout = MQTT_CONN_TIMEOUT - (time.perf_counter() - ts)
            if remaining_timeout <= 0:
                break;
        logging.debug("connectivity attempt finished %s" % "successfully" if self.mqtt_connected else "with error")

        if self.mqtt_connected:
            logging.debug("mqtt connect notification received")
            logging.debug("subscribing to " + self.dev_path + " TRansparent control topics")
            self.client.subscribe(self.dev_path + NCTL_COMMAND)
            self.client.subscribe(self.dev_path + NCTL_ID)
            self.client.subscribe(self.dev_path + NCTL_DATA)
            if self.clear_input(1, False) == 0:
                logging.error("it seems that " + self.otdevice + " could not be controlled over mqtt, no respective transparent control topics exist")
                return -7, "No mqtt controls for device \'%s\' found" % self.otdevice
            logging.info("mqtt opentherm device \'%s\' found, transparent control topics subscribed" % self.otdevice)
            if self.verbose:
                print("Opentherm device \'%s\' found in mqtt" % self.otdevice)
            self.connected = True
            return 1, "Ok"
        else:
            return -7, "Could not connect to mqtt"

    def disconnect(self):
        if self.connected:
            logging.debug("stopping mqtt polling loop...")
            self.client.loop_stop()
            logging.debug("disconnecting mqtt...")
            self.client.disconnect();
            self.connected = False
            self.mqtt_connected = False
            logging.debug("mqtt disconnect completed")

    def connected(self):
        return self.connected

    #
    # Send command to Nevoton's opentherm module through mqtt
    #
    def send_cmd(self, cmd, cmdid, prm):
        if not self.connected:
            logging.error("calling of send_cmd on non-connected interface")
            return -100, "Not connected"
        self.clear_input(0, True)
        logging.info("sending cmd %d with dataid %d and param %d" % (cmd, cmdid, prm))
        self.client.publish(self.dev_path + NCTL_COMMAND + NCTL_SUFFIX, str(cmd))
        self.client.publish(self.dev_path + NCTL_ID + NCTL_SUFFIX, str(cmdid))
        self.client.publish(self.dev_path + NCTL_DATA + NCTL_SUFFIX, str(prm))
        ts = time.perf_counter()
        got_cmd_replies = 0
        got_data_replies = 0
        cmd_response = ""
        cmd_response_data = ""
        remaining_timeout = MQTT_PartResponseTimeout
        logging.debug("starting wait loop with timeout of %d secs" % remaining_timeout)
        while (True):
            try:
                r = self.replyQ.get(True, remaining_timeout)
                logging.debug("got queued item (replies %d/%d): t=%s (%d secs passed), topic=%s, d=%s" % (got_cmd_replies, got_data_replies, time.strftime("%H:%M:%S", time.localtime(r.arrivalTime)), int(time.perf_counter() - ts), r.replyTopic, r.replyData ))
                self.save_msgdata(r)
                replyStr = str(r.replyData.decode("utf-8"))
                if NCTL_COMMAND in r.replyTopic:
                    got_cmd_replies += 1
                    if got_cmd_replies == 1:
                        if replyStr == str(cmd):
                            logging.debug("got command processing reply")
                        else:
                            logging.warning("got invalid command %d(%d,%d) processing reply %s (expected %s)" % (cmd, cmdid, prm, replyStr, str(cmd)))
                            logging.debug("response received in %d seconds after command" % int(time.perf_counter() - ts))
                            return -2, "command validation error"
                    elif got_cmd_replies == 2:
                        if replyStr == "0":
                            logging.debug("got command confirmation reply")
                        elif replyStr == "1":
                            logging.error("got nevoton command %d(%d,%d) validation error %s" % (cmd, cmdid, prm, replyStr))
                            logging.debug("response received in %d seconds after command" % int(time.perf_counter() - ts))
                            return -2, "invalid nevoton command"
                        elif (cmd == NCMD_READ and replyStr == str(OT_READ_ACK)) or (cmd == NCMD_WRITE and replyStr == str(OT_WRITE_ACK)):
                            # it happens time to time...
                            logging.warning("got unsolicited command %d(%d,%d) response %s (assume valid confirmation)" % (cmd, cmdid, prm, replyStr))
                            cmd_response = replyStr
                        elif (replyStr == str(OT_DATA_INVALID)) or (replyStr == str(OT_UNKNOWN_DATA_ID)):
                            logging.error("got unsolicited opentherm response %s/%s" % (self.ot_decoder.msg_descr(int(replyStr)), replyStr))
                            logging.debug("response received in %d seconds after command" % int(time.perf_counter() - ts))
                            return -1, "got %s/%s response" % (self.ot_decoder.msg_descr(int(replyStr)), replyStr), int(replyStr), -1
                        else:
                            logging.error("got unexpected command %d(%d,%d) response %s" % (cmd, cmdid, prm, replyStr))
                            logging.debug("response received in %d seconds after command" % int(time.perf_counter() - ts))
                            return -2, "invalid response"
                    else:
                        if cmd_response != "":
                            logging.error("got unexpected second cmd %d(%d,%d) response %s (first was %s)" % (cmd, cmdid, prm, replyStr, cmd_response))
                        else:
                            cmd_response = replyStr
                            if (cmd == NCMD_READ and cmd_response == str(OT_READ_ACK)) or (cmd == NCMD_WRITE and cmd_response == str(OT_WRITE_ACK)):
                                logging.info("got opentherm response %s/%s" % (self.ot_decoder.msg_descr(int(cmd_response)), cmd_response))
                                pass
                            elif (replyStr == str(OT_DATA_INVALID)) or (replyStr == str(OT_UNKNOWN_DATA_ID)):
                                logging.error("got error opentherm response %s/%s" % (self.ot_decoder.msg_descr(int(replyStr)), replyStr))
                                logging.debug("response received in %d seconds after command" % int(time.perf_counter() - ts))
                                return -1, "got %s/%s response" % (self.ot_decoder.msg_descr(int(replyStr)), replyStr), int(replyStr), -1
                            else:
                                logging.error("got invalid opentherm response \'%s\' on cmd %d with dataid %d and prm %d" % (replyStr, cmd, cmdid, prm))
                                logging.debug("response received in %d seconds after command" % int(time.perf_counter() - ts))
                                return -2, "invalid opentherm response (%s)" % cmd_response
                elif NCTL_ID in r.replyTopic:
                    # actually does not arrive at all if "0" was sent to "TR ID/on"
                    if replyStr == str(cmdid):
                        logging.debug("got command id processing reply")
                    if replyStr == "0":
                        logging.debug("got command id confirmation reply")
                elif NCTL_DATA in r.replyTopic:
                    got_data_replies += 1
                    if got_data_replies == 1:
                        if replyStr == str(prm):
                            logging.debug("got command data processing reply")
                        else:
                            if (cmd == NCMD_READ and cmd_response == str(OT_READ_ACK)) or (cmd == NCMD_WRITE and cmd_response == str(OT_WRITE_ACK)):
                                # workaround: if there were neither command data processing reply nor got command data confirmation reply but command ACK is already here
                                cmd_response_data = replyStr
                                logging.info("got (unlikely) supposed opentherm response data \'%s\'" % cmd_response_data)
                            elif got_cmd_replies >= 2:
                                # workaround: if there were neither command data processing reply nor got command data confirmation reply but there were several command confirmations
                                cmd_response_data = replyStr
                                logging.info("got (highly unlikely) supposed opentherm response data \'%s\'" % cmd_response_data)
                            else:
                                logging.error("got cmd %d(%d,%d) invalid data processing reply \'%s\'" % (cmd, cmdid, prm, replyStr))
                                logging.debug("response received in %d seconds after command" % int(time.perf_counter() - ts))
                                return -2, "command validation error"
                    elif got_data_replies ==2:
                        if replyStr == "0":
                            logging.debug("got command data confirmation reply")
                        else:
                            # workaround: if TR Data confirmation "0" does not arrive at all
                            cmd_response_data = replyStr
                            logging.warning("got supposed opentherm response data \'%s\'" % cmd_response_data)
                    else:
                        if cmd_response_data != "":
                            logging.error("got second cmd %d(%d,%d) response data \'%s\' (first was \'%s\')" % (cmd, cmdid, prm, replyStr, cmd_response_data))
                        else:
                            cmd_response_data = replyStr
                            logging.debug("got opentherm response data \'%s\'" % cmd_response_data)
                else:
                    logging.warning("got unexpected mqtt msg on %s with %s in reply to cmd %d(%d,%d)" % (r.replyTopic, replyStr, cmd, cmdid, prm))
                if (cmd_response != "") and (cmd_response_data != ""):
                    logging.info("got full opentherm response in %d seconds after command" % int(time.perf_counter() - ts))
                    try:
                        cmd_response_n = int(cmd_response)
                    except:
                        logging.error("exception on conversion of cmd response \'%s\' to number" % cmd_response)
                        return -2, "non-numeric response cmd (%s)" % cmd_response
                    try:
                        cmd_response_data_n = int(cmd_response_data)
                    except:
                        logging.error("exception on conversion of cmd response data \'%s\' to number" % cmd_response_data)
                        return -2, "non-numeric response data (%s)" % cmd_response_data
                    if (cmd == NCMD_READ and cmd_response == str(OT_READ_ACK)) or (cmd == NCMD_WRITE and cmd_response == str(OT_WRITE_ACK)):
                        logging.info("Returning successful response %d and data %d" % (cmd_response_n, cmd_response_data_n))
                        return 1, "ok", cmd_response_n, cmd_response_data_n
                    elif (cmd_response == str(OT_DATA_INVALID)) or (cmd_response == str(OT_UNKNOWN_DATA_ID)):
                        logging.info("got opentherm response %s/%d" % (self.ot_decoder.msg_descr(cmd_response_n), cmd_response_n))
                        return -1, "got %s/%d response" % (self.ot_decoder.msg_descr(cmd_response_n), cmd_response_n), cmd_response_n, cmd_response_data_n
                    else:
                        logging.info("got erroneous opentherm response %s/%d with data %d" % (self.ot_decoder.msg_descr(cmd_response_n), cmd_response_n, cmd_response_data_n))
                        return -3, "got %s/%d erroneous response" % (self.ot_decoder.msg_descr(cmd_response_n), cmd_response_n), cmd_response_n, cmd_response_data_n

            except queue.Empty as err:
                if time.perf_counter() - ts >= MQTT_PartResponseTimeout:
                    if ((cmd == NCMD_READ and cmd_response == str(OT_READ_ACK)) or (cmd == NCMD_WRITE and cmd_response == str(OT_WRITE_ACK))) and (self.tr_id == str(cmdid)):
                        logging.info("Treat saved data \'%s\' as received from opentherm slave" % self.tr_data)
                        try:
                            cmd_response_n = int(cmd_response)
                        except:
                            logging.error("exception on conversion2 of cmd response \'%s\' to number" % cmd_response)
                            return -2, "non-numeric response cmd (%s)" % cmd_response
                        try:
                            cmd_response_data_n = int(self.tr_data)
                        except:
                            logging.error("exception on conversion2 of saved cmd response data \'%s\' to number" % self.tr_data)
                            return -2, "non-numeric response data (%s)" % self.tr_data
                        logging.info("Returning successful response %d and data %d" % (cmd_response_n, cmd_response_data_n))
                        return 1, "ok", cmd_response_n, cmd_response_data_n
                        
                if ((got_cmd_replies + got_data_replies == 0) & (time.perf_counter() - ts >= MQTT_ReplyTimeout)):
                    logging.error("absolutely no response from opentherm driver on cmd %d" % cmd);
                    return -7, "No response from Nevoton driver"

            remaining_timeout = MQTT_PartResponseTimeout - (time.perf_counter() - ts)
            if remaining_timeout < 0:
                remaining_timeout = MQTT_ResponseTimeout - (time.perf_counter() - ts)
            if remaining_timeout <= 0:
                logging.error("no response from opentherm device to cmd %d(%d,%d) within %d seconds" % (cmd, cmdid, prm, MQTT_ResponseTimeout))
                return -5, "Nevoton driver response timeout"
            logging.debug("restarting wait loop with timeout of %d secs" % remaining_timeout)


    def process_connect(self, _, userdata, flags, rc):
        if rc == 0:
            logging.info("async mqtt connected ok")
            self.mqtt_connected = True
        else:
            logging.error("async mqtt connect error %s(%d) [%s]" % (connack_string(rc), rc, str(flags)))
            self.mqtt_connected = False
        self.connQ.put(self.mqtt_connected)
        
    def process_disconnect(self, _, userdata, rc):
        self.mqtt_connected = False
        if rc == 0:
            logging.info("async mqtt disconnected ok")
        else:
            logging.error("async mqtt unexpectedly disconnected (%d)" % rc)
        self.connQ.put(self.mqtt_connected)

    # does not actually work at all...
    def process_log(self, _, level, buf):
        logging.debug('async mqtt log: ' + buf)

    def process_mqtt_message(self, _, arg1, arg2=None):
        if arg2 is None:
            msg = arg1
        else:
            msg = arg2
        logging.debug("got mqtt msg on [%s] with [%s]" % (msg.topic, msg.payload))
        self.replyQ.put(Reply(time.time(), msg.topic, msg.payload))

    def __del__(self):
        self.disconnect()


##############
#
# Interact with Nevoton's module through the serial/modbusRTU interface
#
class OTSerialInterfaсe(OpenthermInterface):
    def __init__(self, serial_device, modbus_id, verbose, ot_decoder):
        self.connected = False
        self.device = serial_device
        self.modbus_id = modbus_id
        self.verbose = verbose
        self.ot_decoder = ot_decoder
        pass

    def connected(self):
        return self.connected

    def get_device_id(self):
        return self.device

    def connect(self):
        logging.debug("connecting serial device %s..." % (self.device))
        self.client = ModbusClient(
                    method='rtu',
                    port=self.device,  # serial port device like "/dev/ttyMOD1"
                    # It is believed that the following params could be nailed down due to the nature of Nevoton's device
                    #    framer=ModbusRtuFramer,
                        timeout=10,
                        retries=3,
                    #    retry_on_empty=False,
                        close_comm_on_error=False,
                    #    strict=True,
                    # Serial setup parameters
                        baudrate=19200,
                        bytesize=8,
                        parity="N",
                        stopbits=1,
                    #    handle_local_echo=False,
                )
        result = self.client.connect()
        logging.debug("connect returned %s with %s" % (type(result).__name__, str(result)))
        if result != True:
            return -7, "Could not connect serial device \'%s\'" % (self.device)

        logging.debug("Reading module info regs...")
        result = self.client.read_input_registers(200, 5, unit=self.modbus_id)
        logging.debug("read_input_registers returned %s with %s / %s" % (type(result).__name__, str(result.__dict__), str(result)))
        if result.isError():
            return -7, "Unable to read Nevoton's module info registers through \'%s\', modbusId %d: %s, %s" % (self.device, self.modbus_id, type(result).__name__, str(result))

        r = result.registers
        modulename = "".join((chr(r[n]>>8) if chr(r[n]>>8).isalnum() else "") + (chr(r[n]&255) if chr(r[n]&255).isalnum() else "") for n in range(0,4))
        logging.debug("read modulename \'%s\', fw %d.%02d" % (modulename, r[4]/100, r[4]%100))

        if modulename != "BCG102W":
            return -7, "Unsupported module \'%s\'" % modulename

        if r[4] < 130: # transparent control registers are available in fw 1.30+
            return -7, "Unsupported module firmware version \'%d.%02d\'" % (r[4]/100, r[4]%100)

        logging.info("Connected %s, modbus id %d, detected modulename \'%s\', fw %d.%02d" % (self.device, self.modbus_id, modulename, r[4]/100, r[4]%100))
        if self.verbose:
           print("Opentherm device \'%s\' fw %d.%02d connected through %s (%d)" % (modulename, r[4]/100, r[4]%100, self.device, self.modbus_id))
        self.connected = True
        return 1, "Ok"

    def disconnect(self):
        if self.connected:
            logging.debug("closing connection to %s..." % self.device)
            self.client.close()
            self.connected = False

    def connected(self):
        return self.connected

    def write_reg(self, reg_n, data):
        result = self.client.write_register(reg_n, data, unit=self.modbus_id)
        logging.debug("writing reg %d returned %s with %s / %s" % (reg_n, type(result).__name__, str(result.__dict__), str(result)))
        if result.isError():
            return -2, "Error writing reg 209: %s, %s" % (type(result).__name__, str(result))
        return 1, "ok"

    #
    # Send command to Nevoton's opentherm module through serial/modbusRTU
    #
    def send_cmd(self, cmd, cmdid, prm):
        if not self.connected:
            logging.error("calling of send_cmd on non-connected interface")
            return -100, "not connected"
        logging.info("sending cmd %d with dataid %d and param %d" % (cmd, cmdid, prm))

        r = self.write_reg(209, cmd)
        if r[0] < 0:
        	return r
        r = self.write_reg(210, cmdid)
        if r[0] < 0:
        	return r
        r = self.write_reg(211, prm)
        if r[0] < 0:
        	return r

        ts = time.perf_counter()
        while time.perf_counter() - ts < Modbus_ResponseTimeout:
            result = self.client.read_holding_registers(209, 3, unit=self.modbus_id)
            logging.debug("read_input_registers returned %s with %s / %s" % (type(result).__name__, str(result.__dict__), str(result)))
            if result.isError():
                return -7, "unable to read Nevoton's module registers: %s, %s" % (type(result).__name__, str(result))
            r = result.registers
            if r[0] == 0 and r[1] == 0 and r[2] == 0:
                logging.debug("allzero response read, restart reading cycle")
                continue
            if r[0] == NCMD_VAL_ERROR:
                logging.error("got nevoton command %d(%d,%d) validation error" % (cmd, cmdid, prm))
                logging.debug("response received in %d seconds after command" % int(time.perf_counter() - ts))
                return -2, "invalid nevoton command"
            if (r[0] == OT_DATA_INVALID or r[0] == OT_UNKNOWN_DATA_ID) and (r[1] == cmdid):
                logging.error("got error opentherm response %s/%d" % (self.ot_decoder.msg_descr(r[0]), r[0]))
                logging.debug("response received in %d seconds after command" % int(time.perf_counter() - ts))
                return -1, "got %s/%d response" % (self.ot_decoder.msg_descr(r[0]), r[0]), r[0], r[2]
            if ((cmd == NCMD_READ and r[0] == OT_READ_ACK) or (cmd == NCMD_WRITE and r[0] == OT_WRITE_ACK)) and (r[1] == cmdid):
                logging.info("got full opentherm response in %d seconds after command" % int(time.perf_counter() - ts))
                logging.info("Returning successful response %d and data %d" % (r[0], r[2]))
                return 1, "ok", r[0], r[2]
            logging.error("Got inconsistent Nevoton's module response (%d,%d,%d)" % (r[0], r[1], r[2]))
            return -3, "inconsistent response (%d,%d,%d)" % (r[0], r[1], r[2])

        logging.error("No response from opentherm device to cmd %d(%d,%d) within %d seconds" % (cmd, cmdid, prm, Modbus_ResponseTimeout))
        return -5, "Nevoton driver response timeout"


    def __del__(self):
        self.disconnect()


##############
#
# Main opentherm control logic
#
class OTControl:
    def __init__(self, ot_interface, ot_decoder, verbose=False):
        self.cmd_processor = ot_interface
        self.otdecoder = ot_decoder
        self.verbose = verbose

        # specific data-values for particular data-id read operations (to be updated later)
        self.specPrm = { "000": "65280" }

        logging.debug("OTControl initialized")

    def connect(self):
        return self.cmd_processor.connect()

    def read(self, dataid, retry = False):
        if "/" in dataid:
            inval = dataid.split('/')
            dataid = inval[0]
            prm = inval[1]
        else:
            did = ("00" + dataid)[-3:]
            if did in self.specPrm:
                prm = self.specPrm[did]
            else:
                prm = "0"

        try:
            dataid_n = int(dataid)
        except:
            logging.error("exception on conversion of dataid \'" + dataid + "\' to number")
            return -1, "Non-numeric dataid (%s)" % dataid
        try:
            prm_n = self.otdecoder.parse_val(prm)
        except:
            logging.error("exception on conversion of prm \'" + prm + "\' to number")
            return -1, "Non-numeric prm (%s)" % prm

        if self.verbose:
            print("Reading dataid %d%s..." % (dataid_n, ("/" + str(prm_n)) if prm_n != 0 else ""))
        retries = MQTT_ReadRetries if retry else 1
        rn = 1
        while rn <= retries:
            if rn != 1:
                eprint("Retrying...")
            rn += 1
            r = self.cmd_processor.send_cmd_verbose(NCMD_READ, dataid_n, prm_n)
            if r[0] < 0:
                eprint("Read of dataid %d/%d failed: %s" %(dataid_n, prm_n, r[1]))
                if r[0] == -1 and (r[2] == OT_UNKNOWN_DATA_ID or r[2] == OT_DATA_INVALID):
                    return -r[2], "Opentherm error " + self.otdecoder.msg_descr(r[2])
                elif r[0] < -5:
                    return r[0], r[1]
                else:
                    continue
            else:
                if self.verbose:
                    print("Got dataid %d/%d opentherm read response %d/%s with data %d" % (dataid_n, prm_n, r[2], self.otdecoder.msg_descr(r[2]), r[3]))
                    rd, rc = self.otdecoder.describe_param(dataid_n, "R", prm_n, r[3], True)
                    if (rc < 0):
                        if rc == -3:
                            print("In hex: 0x%04x" %(r[3]) + " / In binary: " + "{0:016b}".format(r[3]))
                        else:
                            return rc, rd
                else:
                    print("%d: %d" % (dataid_n, r[3]))
                return 1, "Ok"
        return -2, "Reading error"


    def write(self, dataid, prm, retry = False):
        try:
            dataid_n = int(dataid)
        except:
            logging.error("exception on conversion of dataid \'" + dataid + "\' to number")
            return -1, "Non-numeric dataid (%s)" % dataid
        try:
            prm_n = self.otdecoder.parse_val(prm)
        except:
            logging.error("exception on conversion of prm \'" + prm + "\' to number")
            return -1, "Invalid prm value (%s)" % prm
        if self.verbose:
            print("Writing dataid %d with value %d..." % (dataid_n, prm_n))
        retries = MQTT_WriteRetries if retry else 1
        rn = 1
        while rn <= retries:
            if rn != 1:
                eprint("Retrying...")
            rn += 1
            r = self.cmd_processor.send_cmd_verbose(NCMD_WRITE, dataid_n, prm_n)
            if r[0] < 0:
                eprint("Write of dataid %d with %d failed: %s" %(dataid_n, prm_n, r[1]))
                if r[0] == -1 and (r[2] == OT_UNKNOWN_DATA_ID or r[2] == OT_DATA_INVALID):
                    return -r[2], "Opentherm error " + self.otdecoder.msg_descr(r[2])
                elif r[0] < -5:
                    return r[0], r[1]
                else:
                    continue
            else:
                if self.verbose:
                    print("Got opentherm dataid %d with %d write response %d/%s with data %d" % (dataid_n, prm_n, r[2], self.otdecoder.msg_descr(r[2]), r[3]))
                    rd = self.otdecoder.describe_param(dataid_n, "W", prm_n, r[3], True)
                    if (rd[1] < 0):
                        return -3, "Unexpected data from opentherm device"
                else:
                    print("%d= %d" % (dataid_n, prm_n))
                return 1, "Ok"
        return -2, "Writing error"


    def read_err(self, erridx, retry = False):
        try:
            erridx_n = int(erridx)
        except:
            logging.error("exception on conversion of erridx \'" + erridx + "\' to number")
            return -1, "Non-numeric erridx (%s)" % erridx

        if erridx_n < 0:
            r = self.cmd_processor.send_cmd_verbose(NCMD_READ, 12, 0)
            if r[0] < 0:
                eprint("Read of FHB size failed: %s" %(r[1]))
                if r[0] == -1 and (r[2] == OT_UNKNOWN_DATA_ID or r[2] == OT_DATA_INVALID):
                    return -r[2], "Opentherm error " + self.otdecoder.msg_descr(r[2])
                elif r[0] < -5:
                    return r[0], r[1]
                else:
                    return -1, "Unable to get number of FHB entries"
            else:
                fhbn = ((r[3] >> 8) & 0xff)
                if self.verbose:
                    print("Reading %d FHBs..." % fhbn)
                for fi in range(0, fhbn - 1, 1):
                    self.read_err(str(fi), retry)
                return 1, "Ok"
        else:
           erridx_n = erridx_n & 0xff

        if self.verbose:
            print("Reading Fault History Buffer (FHB) entry %d..." % erridx_n)
        prm = (erridx_n << 8)
        retries = MQTT_ReadRetries if retry else 1
        rn = 1
        while rn <= retries:
            if rn != 1:
                eprint("Retrying...")
            rn += 1
            r = self.cmd_processor.send_cmd_verbose(NCMD_READ, 13, prm)
            if r[0] < 0:
                eprint("Read of TSP id %d failed: %s" %(erridx_n, r[1]))
                if r[0] == -1 and (r[2] == OT_UNKNOWN_DATA_ID or r[2] == OT_DATA_INVALID):
                    return -r[2], "Opentherm error " + self.otdecoder.msg_descr(r[2])
                elif r[0] < -5:
                    return r[0], r[1]
                else:
                    continue
            else:
                if self.verbose:
                    print("Got opentherm response %d/%s with data %d" % (r[2], self.otdecoder.msg_descr(r[2]), r[3]))
                    rd = self.otdecoder.describe_param(11, "R", prm, r[3], True)
                    if (rd[1] < 0):
                        return -3, "Unexpected data from opentherm device"
                else:
                    print("FHB%d: %d" % (erridx_n, r[3]))
                return 1, "Ok"
        return -2, "FHB Reading error"


    def read_tsp(self, tspid, retry = False):
        if len(tspid) == 0:
            tspid = "0"
        if "-" in tspid:
            inval = tspid.split('-')
            tspid = inval[0]
            last_tspid = inval[1]
        else:
            last_tspid = tspid

        try:
            tspid_n = int(tspid)
        except:
            logging.error("exception on conversion of tspid \'" + tspid + "\' to number")
            return -1, "Non-numeric tspid (%s)" % tspid

        if len(last_tspid) == 0:
            last_tspid_n = -1
        else:
            try:
                last_tspid_n = int(last_tspid)
            except:
                logging.error("exception on conversion of last_tspid \'" + last_tspid + "\' to number")
                return -1, "Non-numeric last_tspid (%s)" % last_tspid

        if last_tspid_n < 0:
            r = self.cmd_processor.send_cmd_verbose(NCMD_READ, 10, 0)
            if r[0] < 0:
                eprint("Read of TSP size failed: %s" %(r[1]))
                if r[0] == -1 and (r[2] == OT_UNKNOWN_DATA_ID or r[2] == OT_DATA_INVALID):
                    return -r[2], "Opentherm error " + self.otdecoder.msg_descr(r[2])
                elif r[0] < -5:
                    return r[0], r[1]
                else:
                    return -1, "Unable to get number of TSP entries"
            else:
                tspn = ((r[3] >> 8) & 0xff)
                if self.verbose:
                    print("There are %d TSP registers reported by boiler..." % tspn)
                return self.read_tsp(str(tspid_n) + "-" + str(tspn - 1), retry)

        if (tspid_n >= 0) and (last_tspid_n > 0) and (tspid_n != last_tspid_n): # explicit range of TSPs
            tspid_n = tspid_n & 0xff
            last_tspid_n = last_tspid_n & 0xff
            if self.verbose:
                print("Reading TSPs from %d to %d..." % (tspid_n, last_tspid_n))
            for ti in range(tspid_n, last_tspid_n + 1, 1):
                r = self.read_tsp(str(ti), retry)
                if r[0] < -5:
                    return r[0], r[1]
            return 1, "Ok"

        if self.verbose:
            print("Reading Transparent Slave Parameter (TSP) %d..." % tspid_n)
        prm = (tspid_n << 8)
        retries = MQTT_ReadRetries if retry else 1
        rn = 1
        while rn <= retries:
            if rn != 1:
                eprint("Retrying...")
            rn += 1
            r = self.cmd_processor.send_cmd_verbose(NCMD_READ, 11, prm)
            if r[0] < 0:
                eprint("Read of TSP id %d failed: %s" %(tspid_n, r[1]))
                if r[0] == -1 and (r[2] == OT_UNKNOWN_DATA_ID or r[2] == OT_DATA_INVALID):
                    return -r[2], "Opentherm error " + self.otdecoder.msg_descr(r[2])
                elif r[0] < -5:
                    return r[0], r[1]
                else:
                    continue
            else:
                if self.verbose:
                    print("Got opentherm response %d/%s with data %d" % (r[2], self.otdecoder.msg_descr(r[2]), r[3]))
                    rd = self.otdecoder.describe_param(11, "R", prm, r[3], True)
                    if (rd[1] < 0):
                        return -3, "Unexpected data from opentherm device"
                else:
                    print("TSP%d: %d" % (tspid_n, r[3] & 255))
                return 1, "Ok"
        return -2, "TSP Reading error"


    def write_tsp(self, tspid, tspdata, retry = False):
        try:
            tspid_n = int(tspid) & 0xff
        except:
            logging.error("exception on conversion of tspid \'" + tspid + "\' to number")
            return -1, "Non-numeric tspid (%s)" % tspid
        try:
            tspdata_n = self.otdecoder.parse_val(tspdata) & 0xff
        except:
            logging.error("exception on conversion of tspdata \'" + tspdata + "\' to number")
            return -1, "Non-numeric tspdata (%s)" % tspdata
        if self.verbose:
            print("Writing Transparent Slave Parameter (TSP) %d with %d..." % (tspid_n, tspdata_n))
        prm = (tspid_n << 8) | tspdata_n
        retries = MQTT_WriteRetries if retry else 1
        rn = 1
        while rn <= retries:
            if rn != 1:
                eprint("Retrying...")
            rn += 1
            r = self.cmd_processor.send_cmd_verbose(NCMD_WRITE, 11, prm)
            if r[0] < 0:
                eprint("Write of TSP id %d failed: %s" %(tspid_n, r[1]))
                if r[0] == -1 and (r[2] == OT_UNKNOWN_DATA_ID or r[2] == OT_DATA_INVALID):
                    return -r[2], "Opentherm error " + self.otdecoder.msg_descr(r[2])
                elif r[0] < -5:
                    return r[0], r[1]
                else:
                    continue
            else:
                if self.verbose:
                    print("Got opentherm response %d/%s with data %d" % (r[2], self.otdecoder.msg_descr(r[2]), r[3]))
                    rd = self.otdecoder.describe_param(11, "W", prm, r[3], True)
                    if (rd[1] < 0):
                        return -3, "Unexpected data from opentherm device"
                else:
                    print("TSP%d= %d" % (tspid_n, r[3] & 255))
                return 1, "Ok"
        return -2, "TSP Writing error"

    def scan(self, retry):
        print("Scanning all known readable data-id of \'" + self.cmd_processor.get_device_id() + "\' device")
        skipId11 = False
        for di in self.otdecoder.otd:
            if len(di) == 3 and "R" in self.otdecoder.otd[di].t_dir:
                if int(di) == 11 and skipId11:
                    logging.debug("skipping reg 11 as far as read_tsp was done");
                    continue
                r = self.read(di, retry)
                if (int(di) == 10) and (r[0] == 1): 
                    logging.debug("Apparently TSP regs could be read" )
                    skipId11 = True
                    self.read_tsp("-1", retry)
                elif r[0] < -5:
                    return r[0], r[1]
        return 1, "Ok"


    def full_scan(self, id_range, retry):
        if len(id_range) == 0:
            id_range = "0-255"
        if "-" in id_range:
            inval = id_range.split('-')
            start_id = inval[0]
            finish_id = inval[1]
        else:
            start_id = id_range
            finish_id = "255"
        try:
            start_id_n = int(start_id) & 0xff
        except:
            logging.error("exception on conversion of start_id \'" + start_id + "\' to number")
            return -1, "Non-numeric start_id (%s)" % start_id
        try:
            finish_id_n = int(finish_id) & 0xff
        except:
            logging.error("exception on conversion of finish_id \'" + finish_id + "\' to number")
            return -1, "Non-numeric finish_id (%s)" % finish_id

        print("Full scanning  of \'" + self.cmd_processor.get_device_id() + "\' device in range " + str(start_id_n) + ".." + str(finish_id_n))
        for di in range(start_id_n, finish_id_n + 1, 1):
            r = self.read(str(di), retry)
            if r[0] < -5:
                return r[0], r[1]
        return 1, "Ok"


    def interactive_cmd(self):
        logging.debug("Entering interactive OTControl mode")
        self.verbose = True
        while True:
            try:
                cmdline = input("Enter command (h for help)> ")
                cmd = re.findall(r'\S+', cmdline)
                if cmd[0] == "scan" or cmd[0] == "s":
                    print("Performing read scan through known Data-Ids...");
                    self.scan(True);
                    continue
                elif cmd[0] == "fullscan" or cmd[0] == "f":
                    print("Performing full read scan...");
                    self.full_scan(cmd[1] if len(cmd) > 1 and cmd[1][0].isdigit() else "", True)
                    continue
                elif cmd[0] == "read" or cmd[0] == "r":
                    print("Reading data-id");
                    self.read(cmd[1] if len(cmd) > 1 and cmd[1][0].isdigit() else "", True)
                    continue
                elif cmd[0] == "write" or cmd[0] == "w":
                    print("Writing data-id");
                    self.write(cmd[1] if len(cmd) > 1 and cmd[1][0].isdigit() else "", cmd[2] if len(cmd) > 2 and cmd[2][0].isdigit() else "", True)
                    continue
                elif cmd[0] == "readtsp" or cmd[0] == "rt":
                    print("Reading Transparent Slave Parameter (TSP)");
                    self.read_tsp(cmd[1] if len(cmd) > 1 and cmd[1][0].isdigit() else "", True)
                    continue
                elif cmd[0] == "writetsp" or cmd[0] == "wt":
                    print("Writing Transparent Slave Parameter (TSP)");
                    self.write_tsp(cmd[1] if len(cmd) > 1 and cmd[1][0].isdigit() else "", len(cmd) > 2 and cmd[2] if cmd[2][0].isdigit() else "", True)
                    continue
                elif cmd[0] == "readerr" or cmd[0] == "re":
                    print("Reading Fault-History-Buffer (FHB) entry");
                    self.read_err(cmd[1] if len(cmd) > 1 and cmd[1][0].isdigit() else "", True)
                    continue
                elif cmd[0] == "help" or cmd[0] == "h":
                    print("Commands supported:")
                    print(" s[can] - scan all known readable opentherm data-id")
                    print(" f[ullscan] [<startId>[-<lastId>]] - perform unconditional read scan")
                    print(" r[ead] [<id>[/<data>]] - read data-id")
                    print(" w[rite] <id> <data> - write data-id with given data")
                    print(" readtsp/rt [<startTSP>[-<finishTSP>]] - read Transparent Slave Parameter")
                    print(" writetsp/wt <id> <data> - write Transparent Slave Parameter")
                    print(" readerr/re [<startFHB>[-<finishFHB>]] - read Fault-History-Buffer entry")
                    print(" quit")
                    continue
                elif cmd[0] == "quit" or cmd[0] == "q":
                    print("Quitting");
                    return 1, "ok"
                else:
                    print("Invalid command \'" + cmd[0] + "\'!");
            except KeyboardInterrupt:
                print("Ctrl-C has been catched. Quitting");
                return 1, "ok"






if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Opentherm control via mqtt", add_help=False)
    parser.add_argument("-t", "--topic", dest="mqtt_device", type=str, help="Nevoton module id as mqtt topic", default="") # wbe2-i-opentherm_11
    parser.add_argument("-h", "--host", dest="host", type=str, help="MQTT host", default="localhost")
    parser.add_argument("-p", "--port", dest="port", type=int, help="MQTT port", default="1883")
    parser.add_argument("-u", "--username", dest="username", type=str, help="MQTT username", default="")
    parser.add_argument("-P", "--password", dest="password", type=str, help="MQTT password", default="")
    parser.add_argument("-m", "--modbusRTU", dest="modbus_device", type=str, help="Modbus/serial device to access Nevoton's module", default="") # /dev/ttyMOD1
    parser.add_argument("-a", "--address", dest="addr", type=int, help="ModbusId", default="11")
    parser.add_argument("-r", "--retry", dest="retry", action="store_true", help="Retry request on non-fatal errors")
    parser.add_argument("-l", "--logfile", dest="logfileName", type=str, help="Log file name", default="notexpl.log")
    parser.add_argument("-c", "--console", dest="conlog", action="store_true", help="Logging to console")
    parser.add_argument("-s", "--syslog", dest="syslog", action="store_true", help="Logging to syslog")
    parser.add_argument("-v", "--verbose", dest="verbose", action="store_true", help="Verbose output")
    parser.add_argument("-d", "--debug", dest="debug", action="store_true", help="Debug logging")
    parser.add_argument(
        "cmd",
        type=str,
        nargs='*',
        help='Command: cmd, scan, fullscan, read, write, readtsp, writetsp, readerr',
    )

    args = parser.parse_args()
    verbose=args.verbose

    if verbose:
        print("Nevoton OpenTherm Explorer utility for Wirenboard. Ver 0.%s beta (C) 2023 MaxWolf" % "$Revision: 339 $".split(' ')[1]);

    h = []
    if args.logfileName != "":
        h.append(logging.FileHandler(args.logfileName, "a"))
    if args.conlog:
        h.append(logging.StreamHandler())
    if args.syslog:
        h.append(logging. SysLogHandler())

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers = h)

    logging.info('===== Starting NOTExplorer')
    try:
        ot_decoder = OTDecoder()

        if args.mqtt_device != "":
            ot_interface = OTMQTTInterfaсe(args.host, args.port, args.username, args.password, args.mqtt_device, verbose, ot_decoder)
        elif args.modbus_device != "":
            ot_interface = OTSerialInterfaсe(args.modbus_device, args.addr, verbose, ot_decoder)
            if args.debug:
                log = logging.getLogger('pymodbus')
                log.setLevel(logging.INFO) # use logging.DEBUG to reveal intimate modbus exchanges
        else:
            eprint("Either -t or -m option should be specified")
            logging.error("Neither mqtt nor serial device specified")
            sys.exit(1)

        otc = OTControl(ot_interface, ot_decoder, verbose)

        logging.info("Started with command: [%s/%s/%s]" % (
            args.cmd[0] if len(args.cmd) >= 1 else "", 
            args.cmd[1] if len(args.cmd) >= 2 else "", 
            args.cmd[2] if len(args.cmd) >= 3 else ""))

        if otc.connect()[0] < 0:
            ex = "Unable to connect: %s" % otc.connect()[1]
            eprint(ex)
            logging.error(ex)
            sys.exit(1)

        result = (-1, "UNDEF")
        if args.cmd[0] == "scan" or args.cmd[0] == "s":
            result = otc.scan(args.retry)
        elif args.cmd[0] == "fullscan" or args.cmd[0] == "f":
            result = otc.full_scan(args.cmd[1] if len(args.cmd) > 1 and args.cmd[1][0].isdigit() else "", args.retry)
        elif args.cmd[0] == "cmd" or args.cmd[0] == "c":
            result = otc.interactive_cmd()
        else:
            argI = 0
            argN = len(args.cmd)
            while argI < argN:
                if args.cmd[argI] == "read" or args.cmd[argI] == "r":
                    if argI + 1 >= argN:
                        result = ( -1, "No dataid to read" )
                    else:
                        result = otc.read(args.cmd[argI+1], args.retry)
                    argI = argI + 2
                elif args.cmd[argI] == "write" or args.cmd[argI] == "w":
                    if argI + 1 >= argN:
                        result = ( -1, "No dataid to write" )
                    else:
                        if argI + 2 >= argN:
                            result = ( -1, "No dataid to write" )
                        else:
                            result = otc.write(args.cmd[argI+1], args.cmd[argI+2], args.retry)
                    argI = argI + 3
                elif args.cmd[argI] == "readtsp" or args.cmd[argI] == "rt":
                    if argI + 1 >= argN:
                        result = ( -1, "No tspid to read" )
                    else:
                        result = otc.read_tsp(args.cmd[argI+1], args.retry)
                    argI = argI + 2
                elif args.cmd[argI] == "writetsp" or args.cmd[argI] == "wt":
                    if argI + 1 >= argN:
                        result = ( -1, "No tspid to write" )
                    else:
                        if argI + 2 >= argN:
                            result = ( -1, "No tsp data to write" )
                        else:
                            result = otc.write_tsp(args.cmd[argI+1], args.cmd[argI+2], args.retry)
                    argI = argI + 3
                elif args.cmd[argI] == "readerr" or args.cmd[argI] == "re":
                    if argI + 1 >= argN:
                        result = ( -1, "No error idx to read" )
                    else:
                        result = otc.read_err(args.cmd[argI+1], args.retry)
                    argI = argI + 2
                else:
                    result = (-1, "Unknown command \'" + args.cmd[argI] + "\'")
                    break
    except KeyboardInterrupt:
        result = (-1, "Keyboard interrupt")

    del otc
    del ot_interface
    del ot_decoder
    if result[0] < 0:
        eprint("Error! " + result[1])
        logging.info("Returning exit code %d" % -result[0])
        sys.exit(-result[0])
    else:
        logging.info("Returning success (exit code 0)")
        sys.exit(0)

# Would you find this program useful and wish to thank the author and to encourage his further creativity, your donations will be gratefully accepted within the following wallets:  
# bitcoin:1CHQYGnuLUh4wwXs8vwii2XtA1eSJ5Pji2
# litecoin:LTC1QGZJ5JGGWK99YMZU5L27DD0LPERECGJV2JTAWZZ
# monero:88Jhztx15PF4QXvDEBJoxuYJaSbqYCfyNdyUJmCJtXWUhmiJho4EW6TiBWiHSsiyHrRe9fWLwb2EjTK6cfKvEdmkAgqQtGW
#
# Original location: https://github.com/sthamster/notexplorer
