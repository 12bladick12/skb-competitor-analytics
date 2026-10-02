import argparse
from pathlib import Path

from .exchange import read_table, validate_rows, csv_bytes, xlsx_bytes
from .storage import Store


def main():
    parser = argparse.ArgumentParser(description="Задания, выгрузка и резервная копия")
    parser.add_argument("--db")
    sub = parser.add_subparsers(dest="command",required=True)
    imp = sub.add_parser("import"); imp.add_argument("file")
    exp = sub.add_parser("export"); exp.add_argument("run_id",type=int); exp.add_argument("file")
    backup = sub.add_parser("backup"); backup.add_argument("file")
    args = parser.parse_args()
    db = Store(args.db)
    if args.command == "import":
        path = Path(args.file)
        rules,errors = validate_rows(read_table(path.read_bytes(),path.name))
        if errors:
            raise SystemExit(str(errors))
        print(db.enqueue(rules))
    elif args.command == "export":
        rows = db.results(run_id=args.run_id)
        if not rows:
            raise SystemExit("Запуск не найден")
        path = Path(args.file)
        path.write_bytes(xlsx_bytes(rows) if path.suffix.lower()==".xlsx" else csv_bytes(rows))
    else:
        db.backup(args.file)
        print("Backup created")


if __name__ == "__main__":
    main()
