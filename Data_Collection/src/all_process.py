"""
    汇总所有流程
"""
import os
from pathlib import Path

# 设置 Playwright 浏览器路径为项目本地目录
project_root = Path(__file__).parent.parent  # 获取项目根目录
playwright_browsers_path = project_root / '.venv' / '.playwright-browsers'
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(playwright_browsers_path)

from src.core.base_spider import BaseSpider
from src.core.wechat_funcs import ArticleDetail
from src.storage.save_to_excel import SaveToExcel
from src.utils.tools import *
from src.storage.save_to_html import SaveWebpageToHtml
import os
from bs4 import BeautifulSoup


class AccessWechatArticle:
    def __init__(self):
        self.base_spider = BaseSpider()     # 获取主页链接
        self.article_detail = ArticleDetail()   # 共用微信token
        self.nickname: str = None
        self.public_token_link: str = None

    def get_public_main_link(self, article_url):
        """
            获取文章的公共号主页链接
        """
        content = self.base_spider.get_an_article(article_url)
        if content['content_flag'] == 1:
            self.base_spider.format_content(content['content'])
            self.nickname = self.base_spider.nickname       # 供后续使用
            public_main_link = self.base_spider.public_main_link
            print(f'公众号名称：{self.nickname}\n公众号主页: ↘ ↘ ↘ ↘\n{public_main_link}')
            print('将此链接 （￣︶￣）↗ ↗ ↗ ↗ 粘贴发送到 "微信PC端-文件传输助手"')
        else:
            print('获取文章内容失败')
            return None

    def get_article_list(self, public_token_link, page_start, page_end=1):
        """
            代码请求获取文章列表
        """
        # 检查输入参数是否合法
        access_token = self.article_detail.format_raw_link(public_token_link)
        if not access_token:
            print('请检查输入参数是否正确')
            return None
        print('参数齐全，开始获取文章信息，默认状态获取全部文章')
        self.public_token_link = public_token_link  # 供其他功能使用
        # 获取文章列表 [[temproary_page, local_time, create_time, article_title, content_cover, content_url, format_url]]
        list_info = None    
        try:
            if page_start == 0 and page_end == 1:
                list_info = self.article_detail.whole_article_list(0,0)
            elif page_start > page_end and page_end == 1:
                print('防呆输入，已自动交换页码')
                list_info = self.article_detail.whole_article_list(page_end, page_start)
            else:
                list_info = self.article_detail.whole_article_list(page_start, page_end)
        except:
            print('获取文章列表失败')

        # 保存操作, 先获取公众号名称
        if self.nickname is None and list_info is not None:
            # 获取公众号名称
            article_url = list_info[0][6]
            content = self.base_spider.get_an_article(article_url)
            if content['content_flag'] == 1:
                self.base_spider.format_content(content['content'])
                self.nickname = self.base_spider.nickname
        elif self.nickname is not None:
            print('已检测到公众号名称: ' + self.nickname + '\n')
        else:
            print('未获取到文章列表, 请检查!!!')
            return None
        
        # 存储文章列表到excel
        if list_info is not None:
            # 创建公众号数据存储路径
            nickname_path = set_nickname_path(self.nickname)
            # 实例化存储对象
            save_to_excel = SaveToExcel(nickname_path)
            # 数据表头
            article_list_columns = ['临时页码', '本地保存时间', '文章发布时间', '文章标题', \
                                    '文章封面链接', '文章原始链接（直接访问会提示验证）', '文章直连链接']  # 列名
            # 保存数据
            save_to_excel.save_article_content(save_to_excel.article_raw_path, article_list_columns, list_info)
            print('文章列表保存成功')
            return None
        else:
            print('未获取到文章列表, 请检查!!!')
            return None

    def save_article_content(self, nickname=None):
        """
            保存已有的文章列表内容
            输入：
                公众号名称(如已获取过主页链接, 则跳过输入)
                默认认为已获取文章列表
            输出：
                无(文章内容保存到Excel文件中)
        """
        if nickname == '' and self.nickname is None:
            print('检测到当前会话未涉及公众号信息获取操作!!!')
            print('请输入需要保存的公众号名称')
            return None
        elif nickname == '' and self.nickname is not None:
            print('已检测到公众号名称: ' + self.nickname + '\n')
            nickname = self.nickname    # 设定公众号名称为局部变量
        else:
            print('当前输入公众号名称: ' + nickname + '\n')
            self.nickname = nickname    # 设定公众号名称为全局变量

        
        # 创建公众号数据存储路径
        nickname_path = set_nickname_path(self.nickname)
        # 读取文章列表
        article_list = SaveToExcel(nickname_path).read_article_list()
        if article_list is None:
            print('请先获取文章列表, 并确认已保存文章列表到Excel文件中, 再执行此操作')
            return None
        
        # 错误文章列表
        article_error_list = []
        # 遍历文章列表, 保存文章内容
        for article in article_list:
            # 文章发布日期
            create_time = article[2]
            # 文章标题
            article_title = article[3]

            # 文章直连链接
            article_url = article[6]
            # 创建文章数据存储路径
            article_path = set_article_path(nickname_path, create_time, article_title)
            
            # 保存html内容到本地
            save_flag = SaveWebpageToHtml().save_webpage_with_resources(article_url, article_path)
            if save_flag:
                # 文章内容保存成功, 读取内容后更新excel
                # 读取html内容
                html_path = os.path.join(article_path, 'index.html')
                with open(html_path, 'r', encoding='utf-8') as f:
                    html_content = f.read()

                # 获取文章创建时间
                try:
                    createTime_match = re.search(r"var createTime = '(.*?)'.*", html_content)
                    if createTime_match:
                        createTime = createTime_match.group(1)
                        # 修改文章创建时间
                        article[2] = createTime

                        # 解析并提取文本
                        soup = BeautifulSoup(html_content, 'lxml')
                        # 将文字内容转换为列表形式
                        original_texts = soup.getText().split('\n')  # 将页面所有的文本内容提取，并转为列表形式
                        format_texts = list(filter(lambda x: bool(x.strip()), original_texts))  # filter() 函数可以根据指定的函数对可迭代对象进行过滤
                        # 添加文本内容到指定文章列表数据中
                        article.append(str(format_texts))
                        
                        # 实例化存储对象
                        save_to_excel = SaveToExcel(nickname_path)
                        # 数据表头
                        article_list_columns = ['临时页码', '本地保存时间', '文章发布时间', '文章标题', \
                                        '文章封面链接', '文章原始链接（直接访问会提示验证）', '文章直连链接', '文章内容']  # 列名
                        # 保存数据 - 注意：需要将 article 包装成二维列表格式
                        article_list_savepath = save_to_excel.article_contents_path
                        save_to_excel.save_article_content(article_list_savepath, article_list_columns, [article])
                        print('文章 ' + article_title + ' 保存成功')
                    else:
                        print(f'⚠️  警告: 未能从HTML中提取创建时间，保留原有时间 - 文章: {article_title}')
                        article_list.remove(article)    # 删除当前文章
                        article_error_list.append(article)
                except Exception as e:
                    print(f'⚠️  警告: 提取创建时间时出错，保留原有时间 - 文章: {article_title}, 错误: {str(e)}')
                    article_list.remove(article)    # 删除当前文章
                    article_error_list.append(article)
                
                
            else:
                print('文章内容保存失败')
                print('文章标题: ' + article_title)
                print('文章链接: ' + article_url)
                article_list.remove(article)    # 删除当前文章
                article_error_list.append(article)

        # 实例化存储对象
        save_to_excel = SaveToExcel(nickname_path)
        # 数据表头
        article_list_columns = ['临时页码', '本地保存时间', '文章发布时间', '文章标题', \
                        '文章封面链接', '文章原始链接（直接访问会提示验证）', '文章直连链接']  # 列名
        # 保存数据
        article_list_savepath = save_to_excel.article_error_path
        save_to_excel.save_article_content(article_list_savepath, article_list_columns, article_error_list)

    def save_article_details(self, public_token_link):
        """
            功能描述：
                保存文章的详情数据
            输入：
                微信客户端token
            输出：
                无(文章详情保存到Excel文件中)
        """
        # 检查输入参数是否合法
        access_token = self.article_detail.format_raw_link(public_token_link)
        if not access_token:
            print('请检查输入参数是否正确')
            return None
        print('参数齐全，开始获取文章信息，默认状态获取全部文章')

        # 使用token获取公众号名称
        nickname = self.article_detail.get_detail_nickname()
        # 创建公众号数据存储路径
        nickname_path = set_nickname_path(self.nickname)

        # 实例化存储对象
        save_to_excel = SaveToExcel(nickname_path)
        article_list_path = save_to_excel.article_raw_path      # 文章列表路径

        # 读取文章列表
        article_list = save_to_excel.read_article_list()
        if article_list is None:
            print('请先获取文章列表, 并确认已保存文章列表到Excel文件中, 再执行此操作')
            return None
        
        article_error_list = []
        # 遍历文章列表, 保存文章内容
        for article in article_list:
            # 获取文章内容
            content = self.base_spider.get_an_article(article[6])
            if content['content_flag'] == 1:  # 检查文章内容是否获取成功
                article_content = self.base_spider.format_content(content['content'])
                # 修改文章创建时间
                article[2] = article_content['createTime']
                # 添加格式化后的文章内容
                article.append(str(article_content['format_texts']))
                # 获取文章详情, 仅当文章内容没问题时执行
                article_detail = self.article_detail.get_detail_content(article[5], article[3], content['content'])
                if article_detail is None: article.append('******文章详情获取失败!!!*******')
                else: article.extend(article_detail)    # 批量添加文章详情
            else:
                # print(f'获取文章内容失败, 文章链接: {article[6]}')
                article_list.remove(article)    # 删除当前文章
                article_error_list.append(article)


        # 保存文章内容
        article_list_columns = ['临时页码', '本地保存时间', '文章发布时间', '文章标题', \
                                '文章封面链接', '文章原始链接（直接访问会提示验证）', '文章直连链接', '文章内容', \
                                '阅读量', '点赞数', '转发数', '在看数', '评论数', '评论点赞数']  # 列名
        article_list_savepath = save_to_excel.article_details_path
        save_to_excel.save_article_content(article_list_savepath, article_list_columns, article_list)

        # 保存错误文章列表
        article_list_columns = ['临时页码', '本地保存时间', '文章发布时间', '文章标题', \
                                '文章封面链接', '文章原始链接（直接访问会提示验证）', '文章直连链接']  # 列名
        article_list_savepath = save_to_excel.article_error_path
        save_to_excel.save_article_content(article_list_savepath, article_list_columns, article_error_list)

            