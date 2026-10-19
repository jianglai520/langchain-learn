# LangChain 1.2 学习笔记

基于 **LangChain 1.x** 的系统学习项目：从模型接入、消息与提示词、工具调用、结构化输出，一路到 Agent、中间件、记忆和 RAG。内容按章节组织，每章配套可直接运行的 Jupyter Notebook，并包含本地模型、PostgreSQL 持久化、Milvus 向量库等工程实践。

## 内容概览

- 以 LangChain 1.2 / LangGraph 1.1 的当前 API 为准，不依赖旧版 `Chain` 写法
- 每章独立目录，Notebook 可单独运行，示例贴近真实调用流程
- 覆盖 DeepSeek、智谱、阿里百炼等在线平台，以及 Ollama 本地模型
- 包含 Agent 中间件、短期/长期记忆、RAG 文档处理与向量检索的完整案例
- 附带一份 PostgreSQL 持久化记忆的安装、原理与排错笔记

## 技术栈

| 分类 | 组件 |
| --- | --- |
| 核心框架 | `langchain` 1.2.12、`langchain-core` 1.2.18、`langgraph` 1.1.2 |
| 模型接入 | `langchain-deepseek`、`langchain-openai`、`langchain-anthropic`、`langchain-ollama`、`dashscope` |
| 数据与存储 | PostgreSQL + `langgraph-checkpoint-postgres`、Milvus + `langchain-milvus` |
| 文档解析 | `unstructured`、`pypdf`、`python-docx`、`pandas` |
| 可观测性 | LangSmith |
| 运行环境 | Python 3.12、Jupyter |

完整依赖见 [`requirements_full.txt`](./requirements_full.txt)。

## 章节导航

| 章节 | 主题 | 主要内容 |
| --- | --- | --- |
| [chapter01_summary](./chapter01_summary) | 环境确认 | 安装 LangChain 并验证版本 |
| [chapter02_model](./chapter02_model) | 模型接入与调用 | 在线模型（DeepSeek / 智谱 / 百炼）、`init_chat_model()`、Ollama 本地模型、`invoke` / `stream` / `batch` 及异步版本、模型 profile 与运行时参数 |
| [chapter03-langsmith](./chapter03-langsmith) | LangSmith | 配置追踪，观察模型调用的链路与输入输出 |
| [chapter04-messages_prompt](./chapter04-messages_prompt) | 消息与提示词 | `System/Human/AI/ToolMessage`、`content` 与 `content_blocks`（多模态）、`ChatPromptTemplate`、`partial()`、`MessagesPlaceholder`、可复用模板库 |
| [chapter05-tools](./chapter05-tools) | 工具调用 | Tool 的调用流程、不用 `@tool` 的定义方式、`@tool` 装饰器、`args_schema`、docstring 与参数描述、多工具绑定、`tool_choice` |
| [chapter06-structured_output](./chapter06-structured_output) | 结构化输出 | Pydantic / TypedDict / JSON Schema / `@dataclass` 四种 schema、字段约束与校验、`with_structured_output()` 与输出解析器 |
| [chapter07-Agents](./chapter07-Agents) | Agent | `create_agent`、模型与工具绑定、系统提示词、结构化输出策略（Provider / Auto / ToolStrategy）、错误处理、七种流式输出模式、多功能智能助手实战 |
| [chapter08-Middleware](./chapter08-Middleware) | 中间件 | 内置中间件（摘要、人工介入、PII、TodoList、调用限制、失败回退、工具选择、重试、上下文编辑等）、自定义 Node-style / Wrap-style hooks、hook 执行顺序 |
| [chapter09-memory](./chapter09-memory) | 记忆 | Agent 记忆原理、短期记忆（`InMemorySaver` / `PostgresSaver`）、消息裁剪/删除/摘要、长期记忆 Store API（`put` / `get` / `search` / 语义检索）、Agent 接入长期记忆 |
| [chapter10-RAG](./chapter10-RAG) | RAG | 文档加载器、文本切分器、Embedding 模型、Milvus 的 DDL/DML/DQL 与相似度检索、Atguigu Assistant 客服知识库案例 |

其中 `chapter09-memory` 的 PostgreSQL 安装与持久化细节见：[PostgreSQL安装与持久化记忆.md](./chapter09-memory/PostgreSQL安装与持久化记忆.md)。

## 目录结构

```text
langchain_learn/
├── chapter01_summary/              # 环境确认
├── chapter02_model/                # 模型接入与调用
├── chapter03-langsmith/            # LangSmith 追踪
├── chapter04-messages_prompt/      # 消息与提示词模板
├── chapter05-tools/                # 工具定义与调用
├── chapter06-structured_output/    # 结构化输出
├── chapter07-Agents/               # Agent 与实战
├── chapter08-Middleware/           # 内置 / 自定义中间件
├── chapter09-memory/               # 短期与长期记忆
├── chapter10-RAG/                  # 文档加载、切分、嵌入、向量检索
├── asset/load/                     # RAG 各类型示例文档
├── todo_workspace/                 # 临时练习与 pytest 示例
├── knowledge.txt                   # 客服知识库案例数据
├── requirements_full.txt           # 完整依赖清单
├── main.py                         # PyCharm 默认模板，未参与课程内容
└── README.md
```

## 快速开始

### 1. 创建环境并安装依赖

```bash
conda create -n langchain1.2 python=3.12 -y
conda activate langchain1.2
pip install -r requirements_full.txt
```

`requirements_full.txt` 中安装的是 CPU 版 PyTorch。如果需要 GPU，请先按 CUDA 版本单独安装 `torch` / `torchvision`，再安装其余依赖。

### 2. 配置环境变量

在项目根目录新建 `.env`，按需填写：

```dotenv
# DeepSeek / 兼容接口（大部分章节使用）
DEEPSEEK_API_KEY=your-key
DEEPSEEK_BASE_URL=https://your-api-endpoint

# 阿里百炼 / DashScope（模型与 Embedding）
DASHSCOPE_API_KEY=your-key

# 智谱（chapter02 示例）
ZHIPUAI_API_KEY=your-key
ZHIPUAI_BASE_URL=https://your-api-endpoint

# Tavily 联网搜索（chapter07）
TAVILY_API_KEY=your-key

# MinerU 文档解析（chapter10，按需）
MINERU_API_TOKEN=your-token

# PostgreSQL（chapter09）
DB_URI=postgresql://postgres:postgres@127.0.0.1:5432/langchain

# Milvus（chapter10）
MILVUS_URI=http://localhost:19530
```

代码通过 `load_dotenv(override=True)` 读取项目根目录的 `.env`。`.env` 已被 `.gitignore` 排除，请勿提交真实密钥。

### 3. 启动 Notebook

```bash
jupyter lab
```

在 Jupyter 中进入对应章节目录，打开 `.ipynb` 逐个运行。Notebook 使用相对路径（如 `../asset/load/...`、`../knowledge.txt`），请保持在各章节目录内运行，不要随意修改工作目录。

### 4. 按需启动外部服务

```bash
# Ollama 本地模型（chapter02）
ollama serve
ollama pull deepseek-r1:1.5b

# PostgreSQL（chapter09，Docker 方式）
docker run -d --name pg-langchain \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=langchain \
  -p 5432:5432 \
  -v pgdata_langchain:/var/lib/postgresql/data \
  postgres:16
```

Milvus 需要一个监听 `19530` 端口的服务（standalone 或 Milvus Lite 均可），chapter10 的代码默认连接 `http://localhost:19530`。

## 环境变量说明

| 变量 | 用途 | 涉及章节 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 模型调用密钥 | 02–10 |
| `DEEPSEEK_BASE_URL` | 模型 API 地址，按实际平台填写 | 02–10 |
| `DASHSCOPE_API_KEY` | 阿里百炼模型与 DashScope Embedding | 02、09、10 |
| `ZHIPUAI_API_KEY` / `ZHIPUAI_BASE_URL` | 智谱平台示例 | 02 |
| `TAVILY_API_KEY` | Tavily 联网搜索工具 | 07 |
| `MINERU_API_TOKEN` | MinerU 文档解析 | 10 |
| `DB_URI` | PostgreSQL 连接串 | 09 |
| `MILVUS_URI` | Milvus 服务地址 | 10 |

## 补充说明

- 示例中的 `deepseek-flash` 等模型名只是当前环境使用的名称，请按自己平台的可用模型调整。
- 连接 PostgreSQL 时建议使用 `127.0.0.1` 而不是 `localhost`，避免 Windows 上 IPv6 解析导致的连接卡死。
- `chapter06-structured_output/05-四种模式的验证.ipynb` 与 `chapter08-Middleware/05-其他内置中间件.ipynb` 使用本地假服务，运行前先启动：

  ```bash
  python chapter06-structured_output/05-fake_server.py   # 127.0.0.1:8889
  python chapter08-Middleware/fake_deepseek_server.py    # 127.0.0.1:8889
  ```

- Notebook 运行产生的 `parsed_files/`、`.pytest_cache/`、`__pycache__/`、向量库本地文件等均已被 `.gitignore` 排除。
- `asset/load/` 与 `knowledge.txt` 是 RAG 章节的示例数据，属于项目内容，请保留。
