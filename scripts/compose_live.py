"""Select the optional wallet secret without exposing host metadata to the API.

Compose still loads runtime settings itself. Use the same literal .env grammar as
local tooling solely to select the override; caller environment wins, even empty.
"""
import os
from pathlib import Path
import subprocess
import sys

from env_requirements import read_settings


def main():
    try:
        values = read_settings(Path('.env')) if Path('.env').is_file() else {}
        wallet = os.environ.get('ARCHIVE_WALLET_PATH', values.get('ARCHIVE_WALLET_PATH', ''))
        args = sys.argv[1:]
        check = args[-1:] == ['--check']
        if wallet and (check or 'up' in args or 'run' in args):
            if not Path(wallet).is_file():
                raise SystemExit('ARCHIVE_WALLET_PATH must name an existing file')
            # Docker cannot bind-mount a file this user cannot read; say so by name instead of
            # surfacing the daemon error. In-container readability is reported as the
            # `wallet_unreadable` archive state (on Linux the API runs as uid 10001).
            if not os.access(wallet, os.R_OK):
                raise SystemExit('ARCHIVE_WALLET_PATH must be readable by the user running make')
    except OSError:
        raise SystemExit('Cannot read .env or inspect ARCHIVE_WALLET_PATH') from None
    except UnicodeError:
        raise SystemExit('Invalid .env encoding') from None
    except ValueError as error:
        raise SystemExit(str(error)) from None
    if check:
        return
    # Make places a separator between Compose options and the service command.
    split = args.index('--')
    files = ['-f', 'compose.wallet.yaml'] if wallet else []
    raise SystemExit(subprocess.call(
        ['docker', 'compose', *args[:split], *files, *args[split + 1:]]))


if __name__ == '__main__':
    main()
