Nevoton\'s OpenTherm Explorer
===

NOTExplorer.py - �������, ����������� �������������������� � ��������������� �� ��������� [Opentherm](https://ihormelnyk.com/Content/Pages/opentherm_library/Opentherm%20Protocol%20v2-2.pdf) ����� ������� �����, [����������� ��������� �������](https://nevoton.ru/product/modul-rasshireniya-opentherm-dlya-wiren-board-6/), � ����� ������, �������������� ���� ��������. ������� ������������� ���������� ��������� ��������������, �������������� �������� �������� (�����, ��� ���������� ������� � ������ ������), � ����������� ������ ������������� ������� �/�� ������(�) ��������� Opentherm.

������� �������� � ����� ����������� [WirenBoard](https://wirenboard.com) (��������������� � ������������� �� WirenBoard 6), � ������� ��� ����� ������ ������������� Python3 (��������������� 3.5), � ���������� paho-mqtt (��� ������ ����� mqtt) �/��� pymodbus (��� ������ ����� serial/modbusRTU ���������). ����� �� WB, ��� �������, ��� �����, � ���������� ����� ��������� ����� pip ��� ��������� `apt-get install python3-paho-mqtt` � `apt-get install python3-pymodbus`. ����� ���������� ������� �� ����, �������/�������� �� � ������� ������� wb-mqtt-serial, ������������������ �� ��������� ������ WBE2-I-OPENTHERM ����������. ���� ������� ��������, ���������� ������������ ������� � mqtt �����������, ����� - � serial/modbusRTU.

����� ���������� ������������ ������� `-t` � `-m`. � ������ ������ ���������� mqtt ���������, � ���������� ������� ����� ����� mqtt ������������� ������ ���������� (��������, `-t wbe2-i-opentherm_11`), � �� ������ - ��� ���������� ����������������� ����������, ����� ������� ��������� ��� ���� (��������, `-m /dev/ttyMOD1`). ����������� ����������� ��������� ����������� � mqtt-������� � ������������� modbus ����������.

**��������!** ��� ������������� ���������� ����������������� ������� (���� `-m`) ��������������� ���������� �� ������ �������������� ������� ������ mqtt<->serial (�.�., ��������, `fuser -v /dev/ttyMOD1` �� ������ ���������� �������� wb-mqtt-serial). �� � ��������, ���� �� ������ ������������ mqtt ���������, ���������, ��� ������� ���� ���������� ������� ��������������� � ���� ���������� �������� ����� ����������� ��������� WirenBoard.

������� ��������� �������:
`read <data-id>[/<data-value>]` - ������ ������ �������� ����������
`write <data-id> <data-value>` - ������ ������ �������� ����������
`readtsp <paramStart>[-<ParamFinish>]` - ������ "�����������" ��������� (Transparent Slave Parameter (TSP))
`writetsp <paramN> <ParamValue>` - ������ "�����������" ���������
`readerr <errN>` - ������ ������ �� ������� ����� (Fault-History-Buffer (FHB))
`scan` - ������ ���� ��������� ��������� ����� opentherm �������� ����������
`fullscan [<start-id>[-<finish-id>]]` - �������� ������ ���� ����� opentherm �������� ����������
`cmd` - ������ ������� � ������������� ������, ��� ������� ����� ��������� �������������� �������, ��������� ����, ��� ����������������� ����������

��� �������, ����� `cmd`, ����� �������� � ��������� ������ ��������� ���, � ����� ������������������

��� ������� ����� `-v` (� ����� ��� ������� � ������������� ������), ������� ������� �� ����� ����������� ����������� ��������, � ���������� ��������� �� ����� opentherm ������ � ���������������� ������.

��� ���������� ������� `write` ����� ������������ �������� ��������������, ��� �������������� ��������� ������ � ������, ����������� ��� ���������� ������ opentherm. ��������: `write 8 12.5%F8.8` ��� `write 126 3%HB`

��� ������� ����� `-r` ������� ����� �������� ��������� (�� ���� ���) �������, ���� ��� � ���������� �������� ���������� ������.

��� ������� ����� `-l` ������� ����� ������ �������� ����� ������ � ��������� ���-����, � ��� ��������, �������������, ����� `-d` ���� �������� ����� ��������� ����� ���������� ����������.


������� �������������:

������ ������ ������������ ����� ����� mqtt ��������� � ���������� `wbe2-i-opentherm_11`
`./NOTExplorer.py -t wbe2-i-opentherm_11 -r read 3`

����� ���������� ����� ����� mqtt ���������
`./NOTExplorer.py -t wbe2-i-opentherm_11 -r -l log -d -v write 4 1%HB`

������ ���������� � ��������������� 11 ����� ���������������� ��������� `/dev/ttyMOD1` ������������������ ������ ��: ������ ����� opentherm � ������ 2 (�����) �������� 27, ������ ������ 0 � ������������� ��������� ������������� ����� 0 � 3 �������� ����� ������ (��������� "CH enable" + "OTC active"), ������ ����������� 50 �������� � ������ 1, ������ ����� 3 � 5
`./NOTExplorer.py -m /dev/ttyMOD1 -a 11 -r -l log w 2 27 r 0/1%HB0+1%HB3 w 1 50%F8.8 r 3 r 5`

������ ������������ �� ������ ���� opentherm �����
`./NOTExplorer.py -m /dev/ttyMOD1 -r -l log -d -v f`


P.P.S. ��������� ������ ��������� �������� � https://github.com/sthamster/notexplorer
P.S. �� �, �������, ���������� ����� ���������� � ���������, � ���� ������ OTDecoder, ������������� ��������� `self.otd`, ���, ���������, � ������������� �������� ������ �� ������� opentherm ���������...

