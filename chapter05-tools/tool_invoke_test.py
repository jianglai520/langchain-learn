from langchain.chat_models import init_chat_model
from dotenv import load_dotenv
import os
from rich import print as rprint

# 从.env文件中加载环境变量
load_dotenv(override=True)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL")

# 模型的初始化
model = init_chat_model(
    model="deepseek-flash",
    model_provider="deepseek",
    api_key=DEEPSEEK_API_KEY,
    api_base=DEEPSEEK_BASE_URL,
)

# 声明一个函数
def get_weather(city: str):
    return f"{city}天气晴朗"

# 将这个函数绑定在模型上
model_with_tools = model.bind_tools([get_weather])

# 调用
res = model_with_tools.invoke("北京的天气如何?")
rprint(res)