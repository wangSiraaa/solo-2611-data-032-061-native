#!/usr/bin/env python3
"""启动小型联动分析台。"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("analyticdesk.app:app", host="127.0.0.1", port=8000, reload=False)
