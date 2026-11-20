

## 搜索工具服务

搜索工具按照实际调用次数计费。

| 工具名称 | 说明 | 价格（元/次） |
| :--- | :--- | :--- |
| Search-Std | 智谱自研基础搜索，响应快、性价比高 | 0.01 |
| Search-Pro | 智谱自研专业搜索，提供更高的搜索结果召回率 | 0.03 |
| Search-Pro-Sogou | 搜狗搜索，处理时间短，支持搜狗百科和搜狗问问 | 0.05 |
| Search-Pro-Quark | 夸克搜索，时效性强，覆盖多个行业领域 | 0.05 |

---

## 网络搜索 (Web Search API)

Web Search API 是一个专给大模型用的搜索引擎，在传统搜索引擎网页读取、排序的能力基础上，增强了意图识别能力，返回更适合大模型处理的结果（网页标题、URL、摘要、名称、图标等）。支持意图增强检索、结构化输出和多引擎支持。

**请求接口**

```http
POST /paas/v4/web_search
```

**鉴权方式**

| 参数 | 类型 | 说明 |
| :--- | :--- | :--- |
| Authorization | string (header) | **必填**。标准的 HTTP Bearer 认证方式，在 API Keys 页面获取密钥。 |

---

### 请求参数 (Body)

`Content-Type: application/json`

| 参数名 | 类型 | 必填 | 说明 |
| :--- | :--- | :--- | :--- |
| `search_query` | string | 是 | 需要进行搜索的内容，建议搜索 query 不超过 70 个字符。（最大长度：70） |
| `search_engine` | enum<string> | 是 | 要调用的搜索引擎编码。<br>可选值：<br>• `search_std`：智谱基础版搜索引擎<br>• `search_pro`：智谱高阶版搜索引擎<br>• `search_pro_sogou`：搜狗<br>• `search_pro_quark`：夸克搜索 |
| `search_intent` | boolean | 是 | 是否进行搜索意图识别，默认不执行（`false`）。<br>• `true`：执行搜索意图识别，有搜索意图后执行搜索<br>• `false`：跳过搜索意图识别，直接执行搜索 |
| `count` | integer | 否 | 返回结果的条数。默认 10。可填范围：1-50，最大单次搜索返回 50 条。<br>*注：`search_pro_sogou` 可选枚举值为 10、20、30、40、50。同时指定 `search_domain_filter` 和 `search_recency_filter` 时 count 不生效。* |
| `search_domain_filter` | string | 否 | 用于限定搜索结果的范围，仅返回指定白名单域名的内容（如 `www.example.com`）。支持的引擎：`search_std`、`search_pro`、`search_pro_sogou`。 |
| `search_recency_filter` | enum<string> | 否 | 搜索指定时间范围内的网页。默认 `noLimit`。<br>可选值：`oneDay`（一天内）、`oneWeek`（一周内）、`oneMonth`（一个月内）、`oneYear`（一年内）、`noLimit`（不限）。 |
| `content_size` | enum<string> | 否 | 控制返回网页内容的长短。<br>• `medium`：返回摘要信息，满足基础推理需求。<br>• `high`：最大化上下文，信息量大且详细，适合需要细节的场景。 |
| `request_id` | string | 否 | 请求唯一标识符。由用户端传递，长度 6-64 个字符，建议使用 UUID 格式。若未提供平台将自动生成。 |
| `user_id` | string | 否 | 终端用户的唯一 ID，帮助平台对非法活动进行干预。长度 6-128 个字符。 |

---

### 响应参数 (Response)

**200 成功 (`application/json`)**

| 参数名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `id` | string | 任务 ID |
| `created` | integer | 请求创建时间，是以秒为单位的 Unix 时间戳 |
| `request_id` | string | 请求标识符 |
| `search_intent` | object[] | 搜索意图结果 |
| `search_result` | object[] | 搜索结果 |