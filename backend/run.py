from __future__ import annotations

import uvicorn

"""
@Author  : 小滴课堂 【项目文档均已申请版权，盗用必究】
"""
if __name__ == '__main__':
    uvicorn.run('app.main:app', host='0.0.0.0', port=8000, reload=True)
