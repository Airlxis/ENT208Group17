from playwright.sync_api import sync_playwright
import os
from pathlib import Path


def html_to_pdf(html_path, pdf_path=None):
    """
    将HTML文件转换为PDF
    html_path: HTML文件路径
    pdf_path: 输出PDF路径(可选，默认同名.pdf)
    """

    if pdf_path is None:
        pdf_path = html_path.with_suffix('.pdf')

    with sync_playwright() as p:
        # 启动浏览器(无头模式，后台运行不显示窗口)
        browser = p.chromium.launch(headless=True)

        # 创建页面，模拟移动端(iPhone X)
        page = browser.new_page(
            viewport={'width': 375, 'height': 812},
            device_scale_factor=2
        )

        # 加载HTML文件(需要绝对路径)
        file_url = 'file:///' + os.path.abspath(html_path).replace('\\', '/')
        page.goto(file_url, wait_until='networkidle')

        # 等待2秒确保图片加载(微信图片是懒加载)
        page.wait_for_timeout(2000)

        # 执行JS: 强制加载所有data-src图片(微信特有)
        page.evaluate('''
            document.querySelectorAll('img').forEach(img => {
                if (img.dataset.src && !img.src.includes('http')) {
                    img.src = img.dataset.src;
                }
                // 强制显示图片(防止微信用CSS隐藏)
                img.style.opacity = '1';
                img.style.visibility = 'visible';
            });
        ''')

        # 再等待1秒让图片真正加载
        page.wait_for_timeout(1000)

        # 生成PDF
        page.pdf(
            path=pdf_path,
            format='A4',
            print_background=True,  # 保留背景色
            margin={
                'top': '20px',
                'bottom': '20px',
                'left': '20px',
                'right': '20px'
            }
        )

        browser.close()
        print(f"✓ 转换完成: {pdf_path}")


# 使用示例
if __name__ == "__main__":
    # 修改这里：填入你的HTML文件名
    html_file = Path("D:/Learning_materials/ENT208/WXGZH/Access_wechat_article/all_data/公众号----西浦就业CareerCentre/2026-03-18 升学｜UCLA加州大学洛杉矶分校硕士招生宣讲会（西浦专场）/index.html")  # 你的微信HTML文件

    html_to_pdf(html_file)