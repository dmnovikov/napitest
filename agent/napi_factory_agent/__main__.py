import sys

from .selfupdate import guard

# До импорта остального кода: счётчик запусков новой версии и откат на прежнюю (selfupdate)
guard()

from .main import main  # noqa: E402

sys.exit(main())
