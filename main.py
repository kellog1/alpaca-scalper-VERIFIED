"""Entrypoint: python main.py [--dry-run]

NOTE ON PDT: real cash accounts under $25k are subject to Pattern Day Trader
rules (max 3 day trades per 5 business days). Paper accounts aren't restricted,
but a strategy this active would immediately trip PDT on a small live account.
"""
import argparse
import asyncio

from scalper.bot import ScalperBot
from scalper.config import load_config, load_credentials
from scalper.logger import setup_console_logging


def main():
    parser = argparse.ArgumentParser(description="Aggressive Alpaca paper scalper")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log signals without placing any orders")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.dry_run:
        cfg.raw["execution"]["dry_run"] = True
    setup_console_logging(cfg.raw["logging"]["console_level"])

    creds = load_credentials()
    bot = ScalperBot(cfg, creds)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("Interrupted — exiting. (Open paper positions remain; "
              "restart re-syncs them, or flatten in the Alpaca dashboard.)")


if __name__ == "__main__":
    main()
