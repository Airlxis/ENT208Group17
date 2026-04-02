from playwright.sync_api import sync_playwright
from pathlib import Path


def extract_html_text(html_path):
    """
    提取本地HTML文件中的可读文字，保存为同目录下的同名.txt文件

    Args:
        html_path: HTML文件的完整路径（字符串或Path对象）
    """
    html_file = Path(html_path).resolve()

    if not html_file.exists():
        raise FileNotFoundError(f"找不到文件: {html_file}")

    # 自动生成输出路径：同目录，同名，扩展名改为.txt
    txt_file = html_file.with_suffix('.txt')

    # 将本地路径转为浏览器可识别的file://协议
    file_url = html_file.as_uri()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            # 加载HTML（等待网络空闲，确保JS执行完毕）
            page.goto(file_url, wait_until='networkidle')

            # 按优先级尝试提取主要文字区域（避免导航栏/页脚干扰）
            # 先尝试语义化标签，退化到body兜底
            selectors = ['main', 'article', 'div[role="main"]', '.content', '#content', 'body']
            raw_text = ""

            for selector in selectors:
                if page.locator(selector).count() > 0:
                    raw_text = page.inner_text(selector)
                    if raw_text.strip():
                        break

            # 清理格式：删除多余空行，保留段落结构
            lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
            clean_text = '\n\n'.join(lines)  # 段落间空一行

            # 写入文件
            txt_file.write_text(clean_text, encoding='utf-8')

            print(f"✓ 提取完成")
            print(f"  源文件: {html_file}")
            print(f"  输出文件: {txt_file}")
            print(f"  字符数: {len(clean_text)}")

        finally:
            browser.close()

    return str(txt_file)


# ==================== 使用方式 ====================
if __name__ == "__main__":
    # 直接修改这行路径即可运行
    html_path = "D:/Learning_materials/ENT208/WXGZH/Access_wechat_article/all_data/公众号----西浦就业CareerCentre/2026-03-18 升学｜UCLA加州大学洛杉矶分校硕士招生宣讲会（西浦专场）/index.html"  # ← 改成你的HTML路径

    extract_html_text(html_path)

    # 如需批量处理，取消下面注释：
    """
    folder = Path(r"D:\你的文件夹")
    for html_file in folder.glob("*.html"):
        try:
            extract_html_text(html_file)
        except Exception as e:
            print(f"✗ 处理失败 {html_file.name}: {e}")
    """