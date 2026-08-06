# A 股实时行情站

一个基于 Vinext 和 Cloudflare Worker 的 A 股实时行情查询站，当前支持：

- 巨人网络（002558）
- 国电电力（600795）

## 在线访问

https://uploaded-code-site.zhangjinhao949792.chatgpt.site

## 数据与收录

- 页面由服务器生成，每次访问都会重新获取最新行情。
- 行情数据由 Cloudflare Worker 转发东方财富数据源。
- 站点提供 `/robots.txt` 和 `/sitemap.xml`，方便搜索引擎发现和抓取。

## 本地开发

需要 Node.js `>=22.13.0`。

```bash
npm install
npm run dev
```

构建检查：

```bash
npm run build
```

## 目录说明

- `app/`：页面与样式
- `worker/index.ts`：站点入口、行情接口和 SEO 文件路由
- `worker/stock-quote.js`：行情数据请求逻辑
- `public/`：静态资源
