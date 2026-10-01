from .app import serve

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="商场扶梯启停巡检与封控复开平台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default="data/escalator.db")
    args = parser.parse_args()
    serve(args.host, args.port, args.db)
