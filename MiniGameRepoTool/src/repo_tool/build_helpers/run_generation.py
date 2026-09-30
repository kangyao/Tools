"""Run the existing generator and propagate failures from its os.system calls.

Compatible with the project's bundled Python 3.9. The source generator and BAT
are not modified; their original environment, preset and dev mode are preserved.
"""
import os
import runpy
import sys


def main():
    script, preset = sys.argv[1:3]
    original_system = os.system

    def checked_system(command):
        code = original_system(command)
        if code:
            raise SystemExit(code if os.name == "nt" else os.waitstatus_to_exitcode(code))
        return code

    os.system = checked_system
    sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
    sys.argv = [script, preset, "dev"]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
