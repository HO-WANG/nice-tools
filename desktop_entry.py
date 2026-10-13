"""PyInstaller 단일 EXE용 진입점."""

import sys

if __name__ == "__main__":
    if "--ase-worker" in sys.argv[1:]:
        from nice_tools.ase_worker import main
    else:
        from nice_tools.desktop import main
    raise SystemExit(main())
