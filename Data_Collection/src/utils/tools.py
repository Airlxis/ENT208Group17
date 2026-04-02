"""
    工具模块，包含一些常用的工具函数，如保存内容到缓存文件等
    功能1: 
        save_cache(content)   # 保存内容到缓存文件


"""
import time
import random
import os
import re


# 保存内容到缓存文件, 用于调试
def save_cache(content):
    with open(r'src/cache/test_cache.txt', 'w', encoding='utf-8') as f:
        f.write(content)

# 短延时
def delay_short_time():
    """
        功能描述：
            延时函数, 用于避免频繁请求导致的IP被封禁
        输入：
            无
        输出：
            无
    """
    second_max_num = 1.5
    second_min_num = 0.1
    second_num = random.uniform(second_min_num, second_max_num)
    second_num = round(second_num, 3)   # 保留3位小数
    print('为预防被封禁, 短延时：' + str(second_num) + '秒')

    time.sleep(second_num)

# 长延时
def delay_time():
    """
        功能描述：
            延时函数, 用于避免频繁请求导致的IP被封禁
        输入：
            无
        输出：
            无
    """
    second_max_num = 7
    second_min_num = 3
    second_num = random.uniform(second_min_num, second_max_num)
    second_num = round(second_num, 3)   # 保留3位小数
    print('为预防被封禁,开始延时操作，延时时间：' + str(second_num) + '秒')

    time.sleep(second_num)

# 公众号数据存储路径
def set_nickname_path(
    nickname: str=None,
    rootpath: str='all_data'
    ):
    """
        功能描述：
            创建公众号存储路径
        输入：
            公众号名称, 项目根路径
        输出：
            公众号数据存储路径
    """
    if nickname is None:
        nickname = 'A A A ----临时存储'
    else:
        nickname = "公众号----" + nickname
    nickname_path = os.path.join(rootpath, nickname)
    os.makedirs(nickname_path, exist_ok=True)
    return nickname_path

# 文章数据存储路径
def set_article_path(nickname_path: str, create_time: str, article_title: str):
    """
        功能描述：
            创建文章存储路径
        输入：
            公众号数据存储路径, 文章发布日期, 文章标题
        输出：
            文章数据存储路径
    """
    # 兼容Windows下文件名
    article_title_win = re.sub(r'[\\/*?:"<>|]', '_', article_title)  # 替换Windows下文件名非法字符
    article_title_win = article_title_win.replace('.', '')  # 去除小数点，防止自动省略报错
    create_time = create_time.replace(':', '_')  # 文章发布时间，Windows下文件名不能包含冒号 
    article_save_path = os.path.join(nickname_path, create_time + ' ' + article_title_win)
    os.makedirs(article_save_path, exist_ok=True)
    return article_save_path


if __name__ == '__main__':
    # nickname_path = set_path_nickname()
    # print(nickname_path)
    article_path = set_article_path('all_data', '2024-08-09 12:00:00', '测试文章')
    print(article_path)