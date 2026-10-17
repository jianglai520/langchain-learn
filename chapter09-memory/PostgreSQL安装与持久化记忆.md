# PostgreSQL 安装与 LangChain 短期记忆持久化

> 这份文档记录的是本机真实安装过程，包含踩过的坑和排查方法。
> 目标不是"照着敲一遍能跑"，而是"知道每一步为什么这么做、出问题该往哪看"。

## 环境快照

| 项 | 值 |
|---|---|
| 系统 | Windows 11，Git Bash (MINGW64) |
| Docker | Docker Desktop 4.83.0，Engine 29.6.2 |
| Conda 环境 | `langchain1.2`（Python 3.12） |
| langchain | 1.2.12 |
| langgraph | 1.1.2 |
| langgraph-checkpoint-postgres | 3.0.5 |
| psycopg | 3.3.3 |
| PostgreSQL | 16.15（容器内） |

---

## 一、先搞清楚：为什么需要 PostgreSQL

### 1.1 InMemorySaver 的天花板

`chapter09-memory/02-短期记忆.ipynb` 里用的是：

```python
from langgraph.checkpoint.memory import InMemorySaver
checkpoint = InMemorySaver()
```

`InMemorySaver` 本质是一个 **Python 进程内的字典**。它的问题不在于"不够快"，而在于三条硬约束：

1. **进程一退，记忆清零。** 重启 Jupyter kernel、重跑脚本，对话历史全没了。
2. **跨进程不可见。** 前端服务有多个 worker，用户第 1 轮请求打到 worker A，第 2 轮打到 worker B，B 看不到 A 写的记忆。
3. **多用户无法隔离共享。** 虽然 `thread_id` 能区分会话，但数据只活在单机上，多实例部署就散了。

真实业务里这三条任何一条都会致命。所以"短期记忆"这个词容易误导：**它指的是"对话范围内的记忆"，不代表它只能存在内存里。**

### 1.2 checkpoint 到底是什么

LangGraph 的 Agent 本质是一张图（graph）。执行模型是 **super-step**（超步）循环：每个节点执行完，图的状态（`messages` 列表、中间变量等）就推进一步。

`checkpointer` 做的事：**每个 super-step 结束后，把整个 State 快照写一次。**

```
用户提问
   ↓
[node: model]  ← super-step 1 结束，写快照 cp_1
   ↓
[node: tools]  ← super-step 2 结束，写快照 cp_2
   ↓
[node: model]  ← super-step 3 结束，写快照 cp_3
   ↓
返回
```

所以一次带工具调用的 `invoke` 可能产生**多个** checkpoint。每轮对话都从最后一个 checkpoint 恢复状态，把新消息接上去。这就是"记忆"的实现原理——**不是模型记住了，是你每次都把历史重新喂给它。**

### 1.3 checkpointer 是可插拔的

LangGraph 把这个接口抽象好了，换存储后端不动机器人逻辑：

| 实现 | 包 | 适用 |
|---|---|---|
| `InMemorySaver` | `langgraph.checkpoint.memory` | 单元测试、教学演示 |
| `SqliteSaver` | `langgraph-checkpoint-sqlite` | 单机小工具、桌面应用 |
| `PostgresSaver` | `langgraph-checkpoint-postgres` | 生产、多实例、要事务和并发 |
| Redis / 自研 | — | 高吞吐会话缓存 |

**关键认知：换 checkpointer = 换一行 import + 换一行构造 + 跑一次 `setup()`。** Agent 定义、工具、prompt 全都不用动。

---

## 二、选型：PostgreSQL 怎么装

三种方案，本机实测后的对比：

| 方案 | 成本 | 数据落点 | 缺点 |
|---|---|---|---|
| **Docker 容器** ✅ | 一条命令，10 秒 | 命名卷 | 需要 Docker Desktop 常驻 |
| 原生 Windows 安装包（EDB） | 下载 ~300MB，GUI 装 | `C:\Program Files\PostgreSQL` | 注册系统服务、开机自启、卸载残留 |
| 虚拟机（VMware/Hyper-V） | 装系统 + 装 PG + 配网络 | VM 磁盘镜像 | 见下 |

**为什么不用虚拟机：**

PG 在 VM 里，Windows 上的 Python 要连过去，你需要：

1. 改 VM 里的 `postgresql.conf`：`listen_addresses = '*'`
2. 改 `pg_hba.conf`：加一行允许宿主机网段的 `host all all 192.168.x.0/24 md5`
3. 配 VM 网络：NAT 要端口转发，Host-Only 要设静态 IP
4. 处理 VM 的防火墙

这四步和 LangChain 一点关系都没有，纯属基建噪声。而且 VM 得一直开着，开发体验很差。

**Docker 的优势**：PG 跑在 Linux 容器里，端口映射到 `127.0.0.1:5432`，Python 侧看到的和"本机装了 PG"完全一样。删掉容器 = 卸载干净，重建 = 10 秒。

---

## 三、搞懂 Docker 的四个概念（不然后面全是玄学）

只讲这篇文档用得到的部分。

### 3.1 镜像（image）vs 容器（container）

```
镜像 postgres:16         容器 pg-langchain
┌─────────────────┐      ┌─────────────────┐
│ 只读文件系统      │ ──▶ │ 只读层（=镜像）   │
│ 预装 PG 16.15    │ 运行 │ + 可写层         │
│ 一堆配置和启动脚本 │      │ （容器内的改动）   │
└─────────────────┘      └─────────────────┘
   相当于"类"                 相当于"实例"
```

- **镜像**：一个打包好的只读模板。`docker pull postgres:16` 就是下载它。
- **容器**：镜像跑起来后的实例。一个镜像可以起 N 个容器。

`docker run` = 创建容器 + 启动。`docker rm` = 删除容器，镜像还在。

### 3.2 端口映射：`-p 5432:5432`

**这是最容易懵的一点。**

容器有自己独立的网络命名空间。容器里的 PG 监听 `5432`，但这个 5432 **只存在于容器内部**，Windows 上的 Python 直接连 `127.0.0.1:5432` 是连不上的。

`-p 宿主机端口:容器端口` 做的事：在宿主机上开一个 5432 端口，把流量转发到容器内的 5432。

```
Windows                          Docker 容器
127.0.0.1:5432  ───转发───▶   pg-langchain:5432
      ↑
  Python 连这里
```

两个数字不一样也没关系，比如 `-p 5433:5432`：宿主用 5433，容器内还是 5432，连接串写 5433。

### 3.3 数据卷：`-v pgdata_langchain:/var/lib/postgresql/data`

容器的"可写层"跟着容器生死。`docker rm` 一执行，可写层就没了，PG 数据全丢。

**卷（volume）** 是 Docker 管理的持久存储，生命周期独立于容器：

```
容器 pg-langchain             卷 pgdata_langchain
     /var/lib/postgresql/data  ──挂载──▶  Windows 上的实际目录
```

效果：

- 容器删了，卷还在 → 重建容器挂同一个卷，数据完整回来
- 卷删了（`docker volume rm`），数据才真的没了

**这条是所有持久化实验的基础。** 记住：你的 checkpoint 数据在卷里，不在容器里。

### 3.4 环境变量：`-e POSTGRES_PASSWORD=...`

官方 postgres 镜像的启动脚本会读这些环境变量做初始化：

| 变量 | 作用 |
|---|---|
| `POSTGRES_PASSWORD` | **必填**，超管密码。不填容器直接退出 |
| `POSTGRES_USER` | 超管用户名，默认 `postgres` |
| `POSTGRES_DB` | 首次启动自动创建的库，默认同 `POSTGRES_USER` |

⚠️ **这些只在数据目录为空（首次初始化）时生效。** 后面你改了 `-e` 的值，对着已有的卷重新起容器，密码和库名都不会变。这是新手最容易困惑的地方：以为改了环境变量就改了密码，结果连不上。

---

## 四、安装全流程

### Step 0 · 启动 Docker Desktop

```bash
docker version
```

**验收**：输出里必须有 `Server:` 段。只有 `Client:` 说明 daemon 没起来。

```
Client: ...          ← CLI 装好了
Server: ...          ← 引擎在跑（关键）
```

### Step 1 · 拉镜像

```bash
docker pull postgres:16
```

**验收**：`Status: Downloaded newer image for postgres:16`

查看本地镜像：`docker images`

> 如果卡在 `Pulling fs layer` 不动，看本文第六节「坑 1」。

### Step 2 · 起容器

Git Bash 一行搞定（cmd/PowerShell 的续行符不同，见第六节坑 3）：

```bash
docker run -d --name pg-langchain -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=langchain -p 5432:5432 -v pgdata_langchain:/var/lib/postgresql/data postgres:16
```

逐段拆解：

```bash
docker run -d                  # -d = detach，后台跑，不占终端
  --name pg-langchain          # 容器名，后续 start/stop/logs/exec 都靠它
  -e POSTGRES_PASSWORD=postgres # 超管密码（首次初始化用）
  -e POSTGRES_DB=langchain     # 自动建库，省掉手动 CREATE DATABASE
  -p 5432:5432                 # 宿主机:容器 端口映射
  -v pgdata_langchain:/var/lib/postgresql/data  # 命名卷 -> 容器数据目录
  postgres:16                  # 用哪个镜像
```

**验收**：

```bash
docker ps
```

`STATUS` 应该是 `Up xx seconds`，`PORTS` 显示 `0.0.0.0:5432->5432/tcp`。

如果是 `Restarting`，说明容器在崩溃重启，`docker logs pg-langchain` 看原因。

### Step 3 · 连进去验证

```bash
docker exec pg-langchain psql -U postgres -d langchain -c "select version();"
```

**验收**：

```
PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2) on x86_64-pc-linux-gnu ...
```

能出这行，说明 **库存在、用户能登、库名对、端口通**，四件事一次全验证了。

交互式进库（Git Bash 可能需要 `winpty` 前缀）：

```bash
winpty docker exec -it pg-langchain psql -U postgres -d langchain
```

进去后常用的：

```sql
\l          -- 列出所有库
\dt         -- 列出所有表
\d 表名      -- 看表结构
\du         -- 列出用户
\q          -- 退出
```

### Step 4 · 确认 Python 驱动

```bash
conda activate langchain1.2
python -c "import psycopg, psycopg_pool; print(psycopg.__version__, psycopg_pool.__version__)"
```

**验收**：`3.3.3 xx.x.x`

**注意版本坑**：`langgraph-checkpoint-postgres` 用的是 **psycopg 3**，不是 psycopg2。两者 API 不兼容，装错了会报 `ModuleNotFoundError: No module named 'psycopg'`（哪怕 psycopg2 装着）。

缺了就装：

```bash
pip install "psycopg[binary]" psycopg-pool
```

`[binary]` 是预编译轮子，省掉本地编译 C 扩展的麻烦。

### Step 5 · 建表

LangGraph 的 checkpoint 表不是自动建的，第一次必须调 `setup()`：

```python
from langgraph.checkpoint.postgres import PostgresSaver

DB_URI = "postgresql://postgres:postgres@127.0.0.1:5432/langchain"

with PostgresSaver.from_conn_string(DB_URI) as checkpointer:
    checkpointer.setup()
```

**验收**：

```bash
docker exec pg-langchain psql -U postgres -d langchain -c "\dt"
```

应该有 4 张表：

```
checkpoint_blobs
checkpoint_migrations
checkpoint_writes
checkpoints
```

本机实测 `checkpoint_migrations` 里有 10 条记录，说明 schema 迁移到第 10 版。

> `setup()` 幂等，重复调用不会破坏数据，也不会重复建表。但生产环境一般只在部署/迁移流程里跑一次，不放在业务代码里。

---

## 五、LangChain 代码改造

### 5.1 三处改动，Agent 逻辑零改动

原来的 notebook：

```python
from langgraph.checkpoint.memory import InMemorySaver
checkpoint = InMemorySaver()

agent = create_agent(model=model, tools=[], checkpointer=checkpoint)
```

改成：

```python
from langgraph.checkpoint.postgres import PostgresSaver

DB_URI = "postgresql://postgres:postgres@127.0.0.1:5432/langchain"

with PostgresSaver.from_conn_string(DB_URI) as checkpointer:
    checkpointer.setup()
    agent = create_agent(model=model, tools=[], checkpointer=checkpointer)
    # ... invoke 全部写在这个 with 里面
```

`create_agent` 的参数名没变（都叫 `checkpointer`），`thread_id` 的用法没变，`agent.get_state(config)` 的用法也没变。

### 5.2 三种写法，按场景选

**写法 A：`with` 块（脚本首选）**

```python
with PostgresSaver.from_conn_string(DB_URI) as checkpointer:
    checkpointer.setup()
    agent = create_agent(model=model, tools=[], checkpointer=checkpointer)
    res = agent.invoke({"messages": [HumanMessage("你好")]}, config=config)
# 出了 with，数据库连接已关闭，checkpointer 不可再用
```

`from_conn_string` 返回的是**上下文管理器**，不是直接返回 saver 实例。这是新手最常见的错误：

```python
# ❌ 错：拿到的是上下文管理器对象
checkpointer = PostgresSaver.from_conn_string(DB_URI)

# ✅ 对：用 with 拿到真正的 saver
with PostgresSaver.from_conn_string(DB_URI) as checkpointer:
    ...
```

**写法 B：Notebook 里手工进上下文（临时调试）**

Notebook 一个一个 cell 跑，`with` 在 cell 结束就关闭了，下一个 cell 用 `agent` 会报连接已关闭。

```python
checkpointer = PostgresSaver.from_conn_string(DB_URI)
checkpointer.__enter__()      # 手工进入
checkpointer.setup()

agent = create_agent(model=model, tools=[], checkpointer=checkpointer)
# 想关的时候：checkpointer.__exit__(None, None, None)
```

能用，但不够优雅，容易忘关。

**写法 C：连接池（Notebook / 服务端首选）**

```python
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from langgraph.checkpoint.postgres import PostgresSaver

pool = ConnectionPool(
    conninfo=DB_URI,
    max_size=10,
    kwargs={
        "autocommit": True,        # 必须
        "row_factory": dict_row,   # 必须
    },
)
checkpointer = PostgresSaver(pool)
checkpointer.setup()

agent = create_agent(model=model, tools=[], checkpointer=checkpointer)

# 程序退出时
# pool.close()
```

`autocommit=True` 和 `row_factory=dict_row` 是 `PostgresSaver` 的**硬性要求**，少一个就会报类型错误或事务错误。官方文档写得很清楚，但很容易漏。

连接池还顺带解决了并发问题：多个请求复用连接，不会每次 invoke 都新建 TCP 连接。

### 5.3 完整可运行示例

```python
import os

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langchain.messages import HumanMessage
from langgraph.checkpoint.postgres import PostgresSaver

load_dotenv(override=True)

model = init_chat_model(
    model="deepseek-flash",
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL"),
)

DB_URI = "postgresql://postgres:postgres@127.0.0.1:5432/langchain"

with PostgresSaver.from_conn_string(DB_URI) as checkpointer:
    checkpointer.setup()

    agent = create_agent(model=model, tools=[], checkpointer=checkpointer)

    config = {"configurable": {"thread_id": "demo-user-1"}}

    for q in ["我叫如玧", "我叫什么？"]:
        res = agent.invoke({"messages": [HumanMessage(q)]}, config=config)
        print(f"user:  {q}")
        print(f"agent: {res['messages'][-1].content}\n")

    state = agent.get_state(config)
    print("当前 thread 累积消息数:", len(state.values["messages"]))
```

### 5.4 `thread_id` 的语义（别搞混）

```python
config = {"configurable": {"thread_id": "demo-user-1"}}
```

- `thread_id` = **会话 ID**，不是用户 ID，也不是进程 ID
- 同一个 `thread_id` + 同一个 DB → 共享同一段历史，跨进程、跨机器、跨重启都一样
- 不同 `thread_id` → 完全隔离，互相看不到
- 实际业务里一般用 `用户ID:会话ID` 或 UUID，比如 `u123:session-20261017`

`thread_id` 相同的两个进程会**同时写同一条会话**，这时靠 DB 的事务和主键保证一致性。反过来，`thread_id` 撞车就是串话，这点要想清楚再设计。

---

## 六、本次安装踩的五个坑（真实复盘）

这一节比上面的步骤更值钱。出问题时按这里的思路排查。

### 坑 1 · `docker pull` 卡在 `Pulling fs layer`，然后 TLS EOF

**现象：**

```
$ docker pull hello-world
Error response from daemon: failed to resolve reference "docker.io/library/hello-world:latest":
failed to do request: Head "https://docker.m.daocloud.io/v2/library/hello-world/manifests/latest?ns=docker.io":
writing response to docker.m.daocloud.io:443: TLS connect to 127.0.0.1:7897: EOF
```

**这条报错要会读。** 拆开看：

| 片段 | 含义 |
|---|---|
| `docker.m.daocloud.io/v2/...?ns=docker.io` | 走的是**镜像源**（daemon 里配的 registry-mirrors），不是官方 Hub |
| `TLS connect to 127.0.0.1:7897` | 尝试连**代理**，7897 是本机 Clash |
| `EOF` | 连接建立后对端直接关闭（握手阶段被掐） |

**分层定位：**

第一层——代理本身通不通？在 Windows 侧直接测（绕开 Docker）：

```bash
curl -x http://127.0.0.1:7897 -sI -m 10 https://registry-1.docker.io/v2/
```

结果：

```
HTTP/1.1 200 Connection established
HTTP/1.1 401 Unauthorized
```

`200 Connection established` = 代理隧道建好了；`401` = 目标服务器正常应答（Docker Registry v2 未认证就返回 401，**这是正常的**）。结论：**代理没问题。**

第二层——镜像源通不通？直连测：

```bash
curl -sI -m 10 https://docker.m.daocloud.io/v2/
# → HTTP/1.1 401 Unauthorized + Server: nginx
```

`401` + nginx 头 = 源是活的。结论：**源也没问题。**

那问题只能在 Docker 引擎这一侧。

**根因（两个叠加）：**

1. Docker Desktop 的 `settings-store.json` 里：

   ```json
   "OverrideProxyHTTP": "http://127.0.0.1:7897",
   "OverrideProxyHTTPS": "https://127.0.0.1:7897"
   ```

   HTTPS 那一行写的是 **`https://`**。用 HTTPS 代理意味着 Docker 要先和代理本身做 TLS 握手——但 Clash 的 7897 是 HTTP CONNECT 代理，收到 TLS 握手直接断开 → `EOF`。GUI 里填地址时补了个 `https://`，就成了这个结果。

2. 即使改成 `http://`，`127.0.0.1` 在 Docker 引擎里也不等于宿主机。**引擎跑在 WSL2 虚拟机里**，那个 `127.0.0.1` 是虚拟机自己的回环地址。

**验证这个判断：**

```bash
docker info --format '{{json .RegistryConfig.Mirrors}}'
# ["https://docker.m.daocloud.io/","https://registry.cn-hangzhou.aliyuncs.com/"]
```

镜像源是配好的、活的，所以**根本不需要代理**。

**解决：** 清空两个 override，走镜像源。

```bash
docker desktop stop          # 先停，否则退出时会覆盖配置文件
```

编辑 `%APPDATA%\Docker\settings-store.json`：

```json
  "OverrideProxyHTTP": "",
  "OverrideProxyHTTPS": "",
  "ProxyHTTPMode": "disabled",
```

```bash
docker desktop start
docker pull postgres:16      # 一次成功
```

**可复用的方法论：**

> Docker 拉不动，永远先问三个问题：
> 1. 走的是官方 Hub 还是镜像源？（看报错里的域名）
> 2. 代理是在 Windows 侧通、还是在引擎 VM 里不通？
> 3. 用 `curl -x` 分别测"代理→目标"和"直连→目标"，把变量一个个排除。
>
> 不要在 Docker 里调代理调到怀疑人生——在 Windows 上用 `curl` 把结论先测出来。

### 坑 2 · `localhost` 解析到 IPv6，连接卡死

镜像拉下来、容器起来了、`docker exec` 也能连，但 Python 一连接就卡住，40 秒都没返回。

**分层验证：**

```bash
timeout 5 bash -c 'cat < /dev/null > /dev/tcp/127.0.0.1/5432' && echo OK
# TCP 127.0.0.1:5432 OK

timeout 5 bash -c 'cat < /dev/null > /dev/tcp/::1/5432' && echo OK
# TCP v6 FAIL
```

`docker ps` 明明显示 `[::]:5432->5432/tcp`，但实际 `::1` 连不上（Windows 上 Docker 的 IPv6 端口映射不可靠）。

而 `localhost` 在这台机器上**优先解析到 `::1`**，psycopg 卡在 IPv6 连接上等超时。

**解决：连接串里写 `127.0.0.1`，永远别写 `localhost`。**

```python
# ❌ 会卡
DB_URI = "postgresql://postgres:postgres@localhost:5432/langchain"

# ✅ 秒连
DB_URI = "postgresql://postgres:postgres@127.0.0.1:5432/langchain"
```

**顺手加超时**，让问题暴露得快一点：

```python
psycopg.connect(DB_URI, connect_timeout=10)
```

或者连接池里加 `kwargs={"connect_timeout": 10}`。

### 坑 3 · Git Bash 的续行符和 TTY

你在 MINGW64 里，有两个 cmd 的肌肉记忆会害你：

**续行符**：cmd 用 `^`，PowerShell 用反引号 `` ` ``，**Git Bash 用 `\`**。跨行命令最省事的写法就是写成一行。

**交互式 TTY**：`docker exec -it` 在 Git Bash 里常报

```
the input device is not a TTY
```

两个解法：

```bash
winpty docker exec -it pg-langchain psql -U postgres -d langchain   # 加 winpty
docker exec pg-langchain psql -U postgres -d langchain -c "..."      # 不用 -it
```

### 坑 4 · 忘了 `setup()`

```
psycopg.errors.UndefinedTable: relation "checkpoints" does not exist
```

`PostgresSaver` 不会自动建表。第一次用必须先 `checkpointer.setup()`。

### 坑 5 · 改 `-e` 环境变量没生效

删容器重建时，如果**挂载的是同一个卷**，`POSTGRES_PASSWORD` / `POSTGRES_DB` 完全不起作用——因为数据目录非空，初始化脚本跳过了。

改密码只能进库改：

```sql
ALTER USER postgres PASSWORD 'newpass';
```

想彻底重来：先 `docker volume rm pgdata_langchain` 再起容器（**数据会没**）。

---

## 七、数据到底存成了什么

这一节回答"持久化"的本质：你的对话在 DB 里长什么样。

### 7.1 四张表

```
checkpoints            ← 快照本体：状态索引 + 基本类型值
checkpoint_blobs       ← 复杂类型值（列表、消息对象）的序列化二进制
checkpoint_writes      ← 任务级中间写入：pending writes / 崩溃恢复用
checkpoint_migrations  ← schema 版本号，setup() 维护
```

#### `checkpoints` — 状态快照索引

| 列 | 类型 | 含义 |
|---|---|---|
| `thread_id` | text | 会话 ID，主键第一部分 |
| `checkpoint_ns` | text | 命名空间，默认 `''`。子图/嵌套图会用到 |
| `checkpoint_id` | text | 快照 ID，UUIDv7 风格（时间有序，如 `1f1ca075-f4fe-6d52-8001-d708986aa705`），主键 |
| `parent_checkpoint_id` | text | 上一代快照 ID → **形成链，可回溯** |
| `type` | text | 序列化格式标识。当前版本不写这一列（`UPSERT` 语句里没有它），恒为 NULL |
| `checkpoint` | jsonb | 快照本体：`v`、`id`、`ts`、`channel_versions`、`channel_values`、`versions_seen`、`updated_channels` |
| `metadata` | jsonb | 运行元数据：`step`（步骤号）、`source`（`input`/`loop`）、`parents` |

主键 `(thread_id, checkpoint_ns, checkpoint_id)`，另有 `thread_id` 单列索引。

**关键点：一个快照的数据被拆在两处。** 规则写在 `PostgresSaver.put()` 里（`langgraph/checkpoint/postgres/__init__.py:301-308`），源码注释就一句话：

```python
# inline primitive values in checkpoint table
# others are stored in blobs table
blob_values = {}
for k, v in checkpoint["channel_values"].items():
    if v is None or isinstance(v, (str, int, float, bool)):
        pass                                            # 基本类型 → 留在 JSONB
    else:
        blob_values[k] = copy["channel_values"].pop(k)   # 其余 → 挪去 blobs
```

读取时再拼回来：

```python
"channel_values": {
    **(value["checkpoint"].get("channel_values") or {}),   # JSONB 里的基本类型
    **self._load_blobs(value["channel_values"]),           # blobs 里的复杂类型
}
```

所以：

| channel 的值类型 | 落在哪 | 能否直接 `select` 看懂 |
|---|---|---|
| `None` / `str` / `int` / `float` / `bool` | `checkpoints.checkpoint->'channel_values'` | ✅ 明文 |
| `list` / `dict` / `HumanMessage` / 任意对象 | `checkpoint_blobs.blob` | ❌ msgpack 二进制 |
| 本轮没变的 channel | 不产生新的 blob 行，`channel_versions` 指回旧版本 | — |

**对你最直接的影响：`messages` 是 `list[BaseMessage]`，属于第二类，所以对话内容全在 `checkpoint_blobs` 里，是二进制，`select` 出来没法看。** 而像 `n=1`、`status='done'` 这种简单状态，直接躺在 JSONB 里明文可查。

这也解释了为什么"只写变化的部分"能做到省空间：`channel_versions` 记录了每个 channel 当前指向哪个版本，没变的 channel 不产生新 blob 行。

#### `checkpoint_blobs` — 复杂类型值

| 列 | 类型 | 含义 |
|---|---|---|
| `thread_id` / `checkpoint_ns` | text | 定位会话 |
| `channel` | text | 哪个通道，比如 `messages`、`__start__` |
| `version` | text | 版本号，格式见下 |
| `type` | text | 序列化类型：`msgpack` / `json` / `empty` |
| `blob` | bytea | **序列化后的二进制值** |

主键 `(thread_id, checkpoint_ns, channel, version)`。

**版本号长这样**：`00000000000000000000000000000003.0.6731688259501198`

```
00000000000000000000000000000003   ← 32 位补零的自增计数器
.0.6731688259501198                ← 16 位随机小数，避免并发撞车
```

来自 `get_next_version()`：`f"{next_v:032}.{next_h:016}"`，其中 `next_v` 是当前版本 +1，`next_h` 是 `random.random()`。所以版本号**既能比较大小（前缀定序），又不会碰撞（后缀随机）**。

**你的对话历史就在这张表的 `blob` 列里，是 msgpack 二进制。** 想看内容得用 Python 反序列化，不能直接 `select` 出人话：

```python
from langgraph.checkpoint.postgres import PostgresSaver

with PostgresSaver.from_conn_string(DB_URI) as cp:
    tup = cp.get_tuple({"configurable": {"thread_id": "demo-user-1"}})
    for m in tup.checkpoint["channel_values"]["messages"]:
        print(type(m).__name__, "|", m.content)
```

`get_tuple()` 已经帮你把 blobs 反序列化并合并好了，这是"用代码看数据库"的正确姿势。

#### `checkpoint_writes` — 任务级写入

| 列 | 类型 | 含义 |
|---|---|---|
| `thread_id` / `checkpoint_ns` / `checkpoint_id` | text | 定位到某个快照 |
| `task_id` | text | 哪个节点的哪次执行 |
| `idx` | integer | 同一 task 内的写入序号 |
| `channel` | text | 写入哪个通道 |
| `blob` | bytea | 写入的值 |
| `task_path` | text | 任务在图中的路径（子图用） |

主键 `(thread_id, checkpoint_ns, checkpoint_id, task_id, idx)`。

**作用**：节点执行是可能失败的。比如 model 节点已经开始输出 token、tools 节点跑到一半报错，这些中间结果先写进 `checkpoint_writes`。恢复时可以判断"这个 task 已经写过哪些结果"，避免重复执行。

它也是 `state.tasks[].pending_writes` 的来源，调试时很有用。

#### `checkpoint_migrations` — 版本表

| 列 | 类型 | 含义 |
|---|---|---|
| `v` | integer | 已应用的迁移版本号 |

`setup()` 读这张表决定跑哪些 `MIGRATIONS`。本机实测到第 10 版。

**升级 `langgraph-checkpoint-postgres` 后必须重跑 `setup()`**，否则新代码查旧 schema 会报缺列。

### 7.2 一次对话在库里长什么样

假设跑了两轮：

```python
config = {"configurable": {"thread_id": "demo-user-1"}}
agent.invoke({"messages": [HumanMessage("我叫如玧")]}, config=config)
agent.invoke({"messages": [HumanMessage("我叫什么？")]}, config=config)
```

**实际观测（本机 probe 数据）**：一个只有 1 个节点的最简图，`invoke` 两次，产生了 **6 条** checkpoint。原因：每次 invoke 至少写两条——一条 `source=input`（收到输入）、一条 `source=loop`（节点执行完）。带工具的 Agent 每多一个节点，就多一两条。

```
checkpoints 表（thread_id = 'demo-user-1'）
┌──────────────────────────────┬──────────────────────────────┬─────────┬──────────────┐
│ checkpoint_id                │ parent_checkpoint_id         │ step    │ source       │
├──────────────────────────────┼──────────────────────────────┼─────────┼──────────────┤
│ 1f1ca075-f4f8-681c-...       │ NULL                         │ -1      │ input        │ ← 第1轮输入
│ 1f1ca075-f4fd-62c0-...       │ 1f1ca075-f4f8-681c-...       │ 1       │ loop         │ ← 第1轮模型产出
│ 1f1ca075-f4fe-6d52-...       │ 1f1ca075-f4fd-62c0-...       │ 2       │ input        │ ← 第2轮输入
│ ...                          │ ...                          │ ...     │ ...          │
└──────────────────────────────┴──────────────────────────────┴─────────┴──────────────┘
```

`metadata` 的真实内容（明文 JSONB，可以直接查）：

```json
{"step": -1, "source": "input", "parents": {}}
{"step": 1,  "source": "loop",  "parents": {}}
```

`step = -1` 表示"刚收到输入，还没执行"；`step = 0,1,2...` 是超步序号。

`checkpoints.checkpoint` 的真实内容（截断）：

```json
{
  "v": 4,
  "id": "1f1ca075-f4fe-6d52-8001-d708986aa705",
  "ts": "2026-10-17T08:47:23.785141+00:00",
  "versions_seen": { "a": { "branch:to:a": "...02..." } },
  "channel_values": { "n": 1, "msg": "hello" },        ← 基本类型，明文
  "channel_versions": {
    "n": "00000000000000000000000000000003.0.6731688259501198",
    "msg": "00000000000000000000000000000003.0.6731688259501198",
    "__start__": "00000000000000000000000000000002.0.12760773032712924"
  },
  "updated_channels": ["n", "msg"]
}
```

对应 `checkpoint_blobs` 里只有 `__start__`（因为它的值是列表，属于复杂类型），`n` 和 `msg` 因为内联了，blobs 里没有对应行。

**换成你的 Agent，`messages` 就是复杂类型**，所以会是：

```
checkpoint_blobs 表
┌──────────┬───────────────────────────────┬─────────┬────────┐
│ channel  │ version                       │ type    │ blob   │
├──────────┼───────────────────────────────┼─────────┼────────┤
│ messages │ 000...0001.0.06417315792469269│ msgpack │ binary │ ← [HumanMessage("我叫如玧")]
│ messages │ 000...0003.0.67316882595011982│ msgpack │ binary │ ← [..., AIMessage("你好如玧")]
│ messages │ 000...0005.0.28239700347622540│ msgpack │ binary │ ← [..., HumanMessage("我叫什么？")]
│ messages │ 000...0006.0.82524568699257310│ msgpack │ binary │ ← [..., AIMessage("你叫如玧")]
└──────────┴───────────────────────────────┴─────────┴────────┘
```

每轮都是"在上一次的 messages 列表后面 append"，所以第二次 invoke 时模型能看到全部历史。**注意 blob 里存的是完整列表，不是增量消息**——所以历史越长，每行越大。这也解释了一个实践问题：长对话下 checkpoint 表会膨胀得很快。

**这就是"记忆"的全部魔法：没有魔法，只是每次都把完整历史重新塞进 prompt。**

### 7.3 亲手验证这套机制

不用 LLM、不花钱，用一个最简图就能把上面的结论全验证一遍：

```python
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.postgres import PostgresSaver
import psycopg, json

DB_URI = "postgresql://postgres:postgres@127.0.0.1:5432/langchain"

class S(TypedDict):
    n: int
    msg: str

def inc(s):
    return {"n": s["n"] + 1, "msg": "hello"}

g = StateGraph(S)
g.add_node("a", inc)
g.add_edge(START, "a")
g.add_edge("a", END)

with PostgresSaver.from_conn_string(DB_URI) as cp:
    app = g.compile(checkpointer=cp)
    app.invoke({"n": 0, "msg": "x"}, {"configurable": {"thread_id": "probe"}})

with psycopg.connect(DB_URI) as c:
    for r in c.execute("""
        select checkpoint_id, parent_checkpoint_id, metadata,
               jsonb_pretty(checkpoint)
        from checkpoints where thread_id='probe' order by checkpoint_id
    """).fetchall():
        print(r[0], r[1], json.dumps(r[2], ensure_ascii=False))
        print(r[3])
    print("blobs:", c.execute(
        "select channel, version, type from checkpoint_blobs where thread_id='probe'"
    ).fetchall())
```

你会看到 `n`/`msg` 明文躺在 JSONB 里、`blobs` 里没有它们的行——亲手确认一次，比读十遍文档管用。

验证完记得清掉：

```python
with PostgresSaver.from_conn_string(DB_URI) as cp:
    cp.delete_thread("probe")
```

### 7.4 实测 SQL

```bash
# 有几个会话，各自多少条 checkpoint
docker exec pg-langchain psql -U postgres -d langchain \
  -c "select thread_id, count(*) from checkpoints group by thread_id order by 2 desc;"

# 看某会话的快照链
docker exec pg-langchain psql -U postgres -d langchain \
  -c "select checkpoint_id, parent_checkpoint_id, metadata->>'step' as step from checkpoints where thread_id='demo-user-1' order by checkpoint_id;"

# 各表体量
docker exec pg-langchain psql -U postgres -d langchain -c "
select 'checkpoints' t, count(*) from checkpoints
union all select 'blobs', count(*) from checkpoint_blobs
union all select 'writes', count(*) from checkpoint_writes;"

# 表和索引占多少空间
docker exec pg-langchain psql -U postgres -d langchain \
  -c "select relname, pg_size_pretty(pg_total_relation_size(relid)) from pg_catalog.pg_statio_user_tables;"
```

---

## 八、三个层次的持久化实验

**做完这一节，你才算真的理解 checkpoint 存在哪。** 建议按顺序做，每步都观察。

### 实验 1 · 进程重启

1. 跑一次脚本，说"我叫如玧"
2. 关掉终端 / 重启 kernel
3. 重新跑，只问"我叫什么？"

**预期**：能答出"如玧"。
**证明**：状态不在内存里，`thread_id` 一样就能接上。

### 实验 2 · 容器重启

```bash
docker stop pg-langchain
docker start pg-langchain
```

再问一遍。

**预期**：记忆仍在。
**证明**：数据在卷里，不在容器的临时文件系统里。

### 实验 3 · 容器重建（两组对照，最重要）

**对照组 A——保留卷：**

```bash
docker rm -f pg-langchain
docker run -d --name pg-langchain -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=langchain -p 5432:5432 -v pgdata_langchain:/var/lib/postgresql/data postgres:16
```

**预期**：记忆还在。

**对照组 B——不挂卷：**

```bash
docker rm -f pg-langchain
docker volume rm pgdata_langchain
docker run -d --name pg-langchain -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=langchain -p 5432:5432 postgres:16
```

**预期**：表都没了，得重新 `setup()`，记忆清零。

两组一对比，"卷 = 数据的真实归属地"这个认知就固化了。

### 实验 4 · 会话隔离

```python
config_a = {"configurable": {"thread_id": "alice"}}
config_b = {"configurable": {"thread_id": "bob"}}
```

在 A 里说名字，去 B 里问，**必须答不出来**。这验证了 `thread_id` 的隔离性。

---

## 九、排错手册

按症状查。

| 症状 | 原因 | 处理 |
|---|---|---|
| `Cannot connect to the Docker daemon` | Docker Desktop 没启动 | 启动它，等托盘变绿；`docker version` 确认有 Server 段 |
| `docker pull` 卡在 `Pulling fs layer` | 镜像源/代理不通 | 见坑 1：`docker info --format '{{json .RegistryConfig.Mirrors}}'`，用 `curl` 分层测 |
| `TLS connect to 127.0.0.1:7897: EOF` | 代理写了 `https://`，或 `127.0.0.1` 在 VM 里指错 | 清空 override，走镜像源 |
| `port is already allocated` | 5432 被占 | `netstat -ano \| findstr :5432` 找进程；或改 `-p 5433:5432`（连接串同步改） |
| `password authentication failed` | 密码与初始化时不一致 | 改 `-e` 对已有卷无效，进库 `ALTER USER postgres PASSWORD '...'` |
| `database "langchain" does not exist` | `POSTGRES_DB` 没生效 | 同上，卷已初始化；手动 `CREATE DATABASE langchain;` |
| 容器 `Restarting` | 缺 `POSTGRES_PASSWORD` 或卷损坏 | `docker logs pg-langchain` |
| Python 连接卡几十秒才失败 | `localhost` 走了 IPv6 | 改 `127.0.0.1`，加 `connect_timeout` |
| `relation "checkpoints" does not exist` | 没跑 `setup()` | 跑一次 `checkpointer.setup()` |
| `ModuleNotFoundError: psycopg` | 装的是 psycopg2 | `pip install "psycopg[binary]" psycopg_pool` |
| `TypeError: ... dict_row` / 事务相关报错 | 连接池缺 `autocommit`/`row_factory` | 见 5.2 写法 C |
| `the input device is not a TTY` | Git Bash 下的 `-it` | 加 `winpty`，或不用 `-it` 改用 `-c` |
| 升级库后报缺列 | schema 没迁移 | 重跑 `setup()` |

---

## 十、日常运维速查

```bash
# —— 生命周期 ——
docker start pg-langchain        # 开机 / Docker Desktop 重启后
docker stop pg-langchain         # 停掉（数据保留）
docker restart pg-langchain
docker rm -f pg-langchain        # 删容器（卷保留）

# —— 观察 ——
docker ps                        # 运行中的容器
docker ps -a                     # 含已停止
docker logs --tail 50 pg-langchain
docker stats pg-langchain        # CPU/内存实时

# —— 进库 ——
winpty docker exec -it pg-langchain psql -U postgres -d langchain
docker exec pg-langchain psql -U postgres -d langchain -c "\dt"

# —— 卷 ——
docker volume ls
docker volume inspect pgdata_langchain
docker volume rm pgdata_langchain    # ⚠️ 数据永久丢失

# —— 备份 / 恢复 ——
docker exec pg-langchain pg_dump -U postgres -d langchain > backup.sql
cat backup.sql | docker exec -i pg-langchain psql -U postgres -d langchain
```

### 开机自启

`settings-store.json` 里 `"AutoStart": false`。想要每次开机自动起 Docker，可以在 Docker Desktop 设置里勾选 **Start Docker Desktop when you sign in**。

### 清理无用数据

```bash
docker system df            # 看占用
docker system prune         # 清停止的容器/悬空镜像/无用网络（不动卷）
docker system prune -a --volumes   # ⚠️ 连卷一起删，谨慎
```

---

## 十一、速查卡片

```
连接串      postgresql://postgres:postgres@127.0.0.1:5432/langchain
容器名      pg-langchain
数据卷      pgdata_langchain
四张表      checkpoints / checkpoint_blobs / checkpoint_writes / checkpoint_migrations
必跑一次    checkpointer.setup()
```

**五条铁律：**

1. 连接串用 `127.0.0.1`，不用 `localhost`
2. `from_conn_string` 是上下文管理器，必须配 `with`
3. 第一次用必跑 `setup()`；升级依赖后重跑
4. 连接池必须给 `autocommit=True` 和 `row_factory=dict_row`
5. 数据在卷里，不在容器里——这是持久化的全部真相

**一个问题自测**（能答上来就算搞懂了）：

> 我把容器 `docker rm` 掉了，重建之后历史对话还在，为什么？
> 如果我把 `-v pgdata_langchain:...` 也去掉再重建，为什么就没了？

---

## 参考

- LangGraph Persistence 官方文档：https://langchain-ai.github.io/langgraph/concepts/persistence/
- `PostgresSaver` 源码：`langgraph/checkpoint/postgres/__init__.py`
- Docker 官方 postgres 镜像说明：https://hub.docker.com/_/postgres
