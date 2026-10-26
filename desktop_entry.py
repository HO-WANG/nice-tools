"""PyInstaller 단일 EXE용 진입점."""

import sys

if __name__ == "__main__":
    if "--ase-worker" in sys.argv[1:]:
        from nice_tools.ase_worker import main
    elif "--iq-worker" in sys.argv[1:]:
        from nice_tools.iq_worker import main
    elif "--comparison-worker" in sys.argv[1:]:
        from nice_tools.local_compare_worker import main
    elif "--text-inspection-worker" in sys.argv[1:]:
        from nice_tools.text_inspection_worker import main
    else:
        from nice_tools.desktop import main
    raise SystemExit(main())
