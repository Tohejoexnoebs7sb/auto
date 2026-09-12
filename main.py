from app.bootstrap import *  # noqa: F401,F403

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("🚀 Starting bot | version=%s", APP_VERSION_LABEL)
    main()
