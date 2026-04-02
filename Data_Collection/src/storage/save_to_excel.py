"""
    爬虫基类模块
    主要通过单篇文章获取信息
    保存文章信息到excel文件
    主要涉及4个文件:
        1. article_list.xlsx
        2. article_contents.xlsx
        3. article_details.xlsx
        4. error_links.xlsx
"""

import pandas as pd  # 修改excel
import os
import time

from src.utils.tools import *


class SaveToExcel():
    """
        功能描述：
            保存文章信息到excel文件
    """
    def __init__(self, nickname_path):
        self.nickname_path = nickname_path
        self.article_raw_path = os.path.join(self.nickname_path, '文章列表 (article_list).xlsx')
        self.article_contents_path = os.path.join(self.nickname_path, '文章内容 (article_contents).xlsx')
        self.article_details_path = os.path.join(self.nickname_path, '文章详情 (article_detiles).xlsx')
        self.article_error_path = os.path.join(self.nickname_path, '问题链接 (error_links).xlsx')

    def read_article_list(self):
        """
            功能描述：
                读取文章列表
            输入：
                文章列表文件路径
            输出：
                文章列表
        """
        if not os.path.exists(self.article_raw_path):
            print('文章列表文件不存在')
            return None
        # 读取文章列表
        article_list = pd.read_excel(self.article_raw_path)
        all_article_list = []
        # 数据清洗
        for index, row in article_list.iterrows():
            if pd.isna(row.iloc[6]):
                print('检测到存在空数据，跳过')
                continue
            else:
                all_article_list.append(row.to_list())
        return all_article_list

    def save_article_content(self, file_path, columns,content_info):
        """
            功能描述：
                保存单篇文章信息到excel文件
            输入：
                文件路径(已经合并好的路径)
                表头 (一维数组) columns = ['本地存储时间', '文章发布时间']  # 列名
                文章信息 (二维数组) content_info
            输出：
                None
        """
        # 创建 or 打开表格，检查文件是否存在，判断不存在时创建表格文件
        article_contents_path = file_path  # 文章内容文件路径
        if not os.path.exists(article_contents_path): 
            pd.DataFrame().to_excel(article_contents_path, index=False)
        frame_df = pd.read_excel(article_contents_path)  # 读取表格内容，默认打开DataFrame对象包含第一个工作表中的数据

        # 将新数据转换为 DataFrame 并添加到现有 DataFrame 的末尾
        new_data_df = pd.DataFrame(content_info, columns=columns)
        df = pd.concat([frame_df, new_data_df], ignore_index=True)

        # 将更新后的数据写入 Excel 文件
        df.to_excel(article_contents_path, index=False)
        local_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())  # 本地时间
        print(local_time + ' 存储路径>>>> ' + article_contents_path)

