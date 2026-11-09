"""Install Windows shortcuts that group with the iCast taskbar button."""
import sys
from pathlib import Path

import pythoncom
from win32com.propsys import propsys, pscon
from win32com.shell import shell, shellcon

from .config import APP_DESCRIPTION, APP_ICON, APP_NAME, WINDOWS_APP_ID


def main():
    launcher = Path(sys.executable).with_name("pythonw.exe")
    if not launcher.is_file():
        raise FileNotFoundError(launcher)

    for folder_id in (shellcon.CSIDL_DESKTOPDIRECTORY, shellcon.CSIDL_PROGRAMS):
        folder = Path(shell.SHGetFolderPath(0, folder_id, None, 0))
        link = pythoncom.CoCreateInstance(
            shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink
        )
        link.SetPath(str(launcher))
        link.SetArguments("-m icast")
        link.SetWorkingDirectory(str(APP_ICON.parent.parent))
        link.SetIconLocation(str(APP_ICON), 0)
        link.SetDescription(APP_DESCRIPTION)

        properties = link.QueryInterface(propsys.IID_IPropertyStore)
        properties.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(WINDOWS_APP_ID))
        properties.Commit()
        link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(folder / f"{APP_NAME}.lnk"), True)


if __name__ == "__main__":
    main()
