#!/usr/bin/env python3
"""Testa a classificação dos discos (marca, linha, tipo físico) a partir dos
metadados que o Windows (Win32_DiskDrive / Get-PhysicalDisk) e o lsblk reportam,
e o reconhecimento de um HD já catalogado pelo nº de série."""
import sys
import tempfile
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ / "scanner"))
import hardware as hw  # noqa: E402

TB = 10**12

# (descrição, info crua, marca, tipo, linha)
CASOS = [
    ("WD Elements portátil (Windows)",
     {"modelo": "WD Elements 25A2 USB Device", "interface": "USB", "tamanho": 2 * TB,
      "pnp": r"USBSTOR\DISK&VEN_WD&PROD_ELEMENTS_25A2&REV_1021\575831314132&0"},
     "Western Digital", "portatil", "Elements"),
    ("WD Elements de 8 TB -> de mesa pela capacidade",
     {"modelo": "WD Elements 25A3 USB Device", "interface": "USB", "tamanho": 8 * TB},
     "Western Digital", "mesa", "Elements"),
    ("WD My Book",
     {"modelo": "WD My Book 25EE USB Device", "interface": "USB", "tamanho": 4 * TB},
     "Western Digital", "mesa", "My Book"),
    ("WD My Passport",
     {"modelo": "WD My Passport 25E2 USB Device", "interface": "USB", "tamanho": 5 * TB},
     "Western Digital", "portatil", "My Passport"),
    ("Seagate Expansion Desk (UASP, aparece como SCSI)",
     {"modelo": "Seagate Expansion Desk SCSI Disk Device", "interface": "SCSI", "barramento": "USB",
      "tamanho": 6 * TB, "pnp": r"SCSI\DISK&VEN_SEAGATE&PROD_EXPANSION_DESK&REV_0915\7&1a2b&0&000000"},
     "Seagate", "mesa", "Expansion Desktop"),
    ("Seagate Backup Plus portátil (BUP)",
     {"modelo": "Seagate BUP Portable SCSI Disk Device", "barramento": "USB", "tamanho": 4 * TB},
     "Seagate", "portatil", "Backup Plus"),
    ("Toshiba Canvio (modelo genérico, marca pelo VEN_)",
     {"modelo": "TOSHIBA External USB 3.0 USB Device", "interface": "USB", "tamanho": 1 * TB,
      "pnp": r"USBSTOR\DISK&VEN_TOSHIBA&PROD_EXTERNAL_USB_3.0&REV_5438\20180629"},
     "Toshiba", "portatil", ""),
    ("Samsung T7 (SSD externo)",
     {"modelo": "Samsung PSSD T7 SCSI Disk Device", "barramento": "USB", "media": "SSD", "tamanho": 1 * TB},
     "Samsung", "ssd_externo", "Portable SSD"),
    ("LaCie Rugged",
     {"modelo": "LaCie Rugged USB-C SCSI Disk Device", "barramento": "USB", "tamanho": 2 * TB},
     "LaCie", "portatil", "Rugged"),
    ("Ponte USB genérica (ASMedia): sem marca, tipo pela capacidade",
     {"modelo": "ASMT 2115 SCSI Disk Device", "barramento": "USB", "tamanho": 3 * TB,
      "pnp": r"SCSI\DISK&VEN_ASMT&PROD_2115&REV_0\7&2c&0&000000"},
     "", "portatil", ""),
    ("HD interno SATA Seagate (código ST)",
     {"modelo": "ST2000DM008-2FR102", "interface": "IDE", "media": "HDD", "barramento": "SATA", "tamanho": 2 * TB},
     "Seagate", "interno_hdd", ""),
    ("SSD interno NVMe",
     {"modelo": "Samsung SSD 980 PRO 1TB", "barramento": "NVMe", "media": "SSD", "tamanho": 1 * TB},
     "Samsung", "interno_ssd", ""),
    ("Pendrive SanDisk",
     {"modelo": "SanDisk Cruzer Blade USB Device", "interface": "USB", "tamanho": 32 * 10**9},
     "SanDisk", "pendrive", "Pendrive"),
    ("Linux (lsblk): WD Elements",
     {"modelo": "Elements 25A2", "fabricante": "WD", "transporte": "usb", "rotacional": True, "tamanho": 2 * TB},
     "Western Digital", "portatil", "Elements"),
    ("Códigos numéricos do Get-PhysicalDisk (BusType 7 = USB, MediaType 4 = SSD)",
     {"modelo": "Generic SSD", "barramento": "7", "media": "4", "tamanho": 2 * TB},
     "", "ssd_externo", ""),
    ("Marca só pelo VID do USB",
     {"modelo": "USB3.0 Disk Device", "interface": "USB", "tamanho": 2 * TB,
      "pnp": r"USB\VID_0BC2&PID_2322\NA8F1234"},
     "Seagate", "portatil", ""),
    ("Sem nenhuma pista", {}, "", "desconhecido", ""),
]


def verificar():
    checks, falhas = [], []
    def ck(nome, got, exp):
        checks.append((nome, got, exp, got == exp))
        if got != exp: falhas.append(nome)

    for desc, info, marca, tipo, linha in CASOS:
        c = hw.classificar(info)
        ck(desc, (c["marca"], c["tipo"], c["linha"]), (marca, tipo, linha))

    c = hw.classificar({"modelo": "WD Elements 25A2 USB Device", "interface": "USB", "tamanho": 2 * TB})
    ck("rótulo legível do tipo", (c["rotulo_tipo"], c["formato"], c["conexao"]), ("HD portátil", "2,5″", "USB"))

    # séries genéricas de pontes USB não servem para reconhecer
    ck("série só de zeros é descartada", hw.serial_util("000000000000"), "")
    ck("série genérica de ponte é descartada", hw.serial_util("0123456789ABCDEF"), "")
    ck("série real é mantida (normalizada)", hw.serial_util(" wx31a8 1b2c3d "), "WX31A81B2C3D")

    # ficha do disco + reconhecimento pelo nº de série
    with tempfile.TemporaryDirectory() as td:
        orig = hw.info_para
        try:
            hw.info_para = lambda c: dict({"modelo": "WD Elements 25A2 USB Device", "serial": "WX31A81B2C3D",
                                           "serial_volume": "9C1F2A33", "interface": "USB", "tamanho": 2 * TB},
                                          **hw.classificar({"modelo": "WD Elements", "interface": "USB", "tamanho": 2 * TB}))
            f = hw.registrar(td, "HD U", "H:\\")
            f2 = hw.registrar(td, "HD U", "H:\\")
        finally:
            hw.info_para = orig
        ck("ficha gravada com marca e série", (f["marca"], f["serial"]), ("Western Digital", "WX31A81B2C3D"))
        ck("cada passagem registra quando o HD foi visto", len(f2["visto_em"]), 2)
        ck("reconhece o HD pelo nº de série (outra letra, outro rótulo)",
           hw.reconhecer(td, {"serial": "WX31A81B2C3D", "rotulo": "NOVO NOME"}), "HD U")
        ck("HD com outra série não é confundido", hw.reconhecer(td, {"serial": "ZZ99887766"}), None)
        ck("série genérica não reconhece por engano", hw.reconhecer(td, {"serial": "000000000000"}), None)
        ck("sem série física: reconhece pelo nº de série do volume",
           hw.reconhecer(td, {"serial": "", "serial_volume": "9C1F2A33"}), "HD U")
    return checks, falhas


def test_hardware():
    _, falhas = verificar()
    assert not falhas, f"Classificação de discos incorreta: {falhas}"


def main():
    checks, falhas = verificar()
    for nome, got, exp, ok in checks:
        print(f"  [{'OK ' if ok else 'FALHA'}] {nome}: obtido={got!r} esperado={exp!r}")
    print()
    print(f"RESULTADO: {'OK — todas passaram' if not falhas else 'FALHOU: '+str(falhas)}")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
