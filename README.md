# dsh-plugin-cnki-lit

用上财机构权限访问中国知网：检索、摘要、原文下载、Zotero 归档、综述素材。

## 原理

```
上财门户 TGC  →  CARSI(SAML)  →  fsso.cnki.net 会话  →  知网机构权限
                                    ↓
                       一次性滑块 → 浏览器信任态（fingerprint Cookie）
                                    ↓
             纯 HTTP 调 kns.cnki.net 接口（比浏览器快 10 倍）
```

- **认证**自动（复用 `sufe-portal-cli` 的门户登录态），有效期约 8 小时
- **信任态**只需人工过一次滑块，之后长期有效
- 下载用浏览器点「PDF下载」，因为知网的下载链路有多层跳转

## 安装

```bash
git clone https://github.com/Wang666645/cnki-lit.git
dsh plugin --profile desktop add ./cnki-lit
```

或在应用内 **Settings → Plugins** 选择该目录。装完重启。

### 配置 Python 解释器

插件要调这个仓库里的 `cnki_lit.py`，所以需要告诉它用哪个 Python：

```yaml
# ~/.dsh/profiles/<profile>/cordis.patch.yml
- id: cnki-lit
  config:
    python: 'C:\Python314\python.exe'   # 装了 requests + playwright 的那个
    script: 'C:\path\to\cnki-lit\cnki_lit.py'
```

不配也能跑，前提是 `python` 在 PATH 上且已 `pip install -r requirements.txt`。

## 首次使用

```
cnki_auth action=login    # 用门户 TGC 换 CARSI 票
cnki_auth action=trust    # 弹浏览器，拖一次滑块
cnki_auth action=status   # 确认 session_ok
```

## 工具

| 工具 | 作用 |
|---|---|
| `cnki_auth` | 登录态检查 / CARSI 换票 / 建立信任态 |
| `cnki_search` | 检索，单次 ≤50 条，支持主题/篇名/关键词/摘要/作者/来源 |
| `cnki_detail` | 单篇摘要、关键词、作者、PDF 链接 |
| `cnki_download` | 下载 PDF 原文（机构订阅范围内） |
| `cnki_collect` | 批量采集 → PDF + RIS，整包导入 Zotero |
| `cnki_review` | 批量摘要素材包，供写文献综述 |

## 已知限制

1. **翻页触发验证码**：`pageNum=2` 必弹滑块。规避方式是按年份/主题切片，每个切片只取第一页。
2. **单页最多 50 条**，`pageSize` 只接受 20 / 50。
3. **本机代理会破坏 TLS**：脚本内已设 `trust_env=False`，不要改成走系统代理。
4. **Zotero 写入**：插件不直连 Zotero 库，只导出 RIS + PDF，用「文件 → 导入」完成归档。这样不受 dsh-zotero 写入开关限制。

## 错误码

| 码 | 含义 | 处理 |
|---|---|---|
| `NO_SESSION` | 无信任态 | `cnki_auth action=trust` |
| `NO_PORTAL` | 门户未登录 | `sufe-portal-cli auth --qrcode` |
| `CARSI_FAILED` | 换票失败 | 门户登录态过期，重新扫码 |
| `TRUST_EXPIRED` | 信任态失效 | 重新 `action=trust` |
| `CAPTCHA_REQUIRED` | 翻页验证码 | 换关键词或缩小范围 |
| `NOT_PDF` | 无下载权限 | 该文献超出订阅范围 |
