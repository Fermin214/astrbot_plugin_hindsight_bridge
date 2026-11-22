import sqlite3


class Database(sqlite3.Connection):
    def __init__(self, *arguments, **options):
        super().__init__(*arguments, **options)
        self.depth = 0

    def __enter__(self):
        if self.depth:
            self.execute(f"SAVEPOINT bridge_{self.depth}")
        elif not self.in_transaction:
            self.execute("BEGIN IMMEDIATE")
        self.depth += 1
        return self

    def __exit__(self, kind, value, traceback):
        self.depth -= 1
        if self.depth:
            name = f"bridge_{self.depth}"
            if kind is not None:
                self.execute(f"ROLLBACK TO {name}")
            self.execute(f"RELEASE {name}")
        elif kind is None:
            try:
                self.commit()
            except BaseException:
                self.rollback()
                raise
        else:
            self.rollback()
        return False
