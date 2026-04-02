"""
使用 Playwright 保存网页及其所有资源到本地
"""
import os
import re
import hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright


class SaveWebpageToHtml:
    def __init__(self):
        self.html_filename: str = "index.html"  # HTML 文件名,默认为 index.html
        self.scroll_to_bottom: bool = True      # 是否滚动到页面底部以触发懒加载,默认 True
        self.scroll_delay: int = 150            # 每次滚动之间的延迟时间(毫秒),默认 150ms

    def save_webpage_with_resources(self,
        url: str,
        output_dir: str = None,
        wait_for_load: bool = True,
        headless: bool = True,
    ):
        """
        使用 Playwright 将网页及所有资源保存到本地
        
        参数:
            url (str): 需要保存的网页 URL
            output_dir (str): 输出目录,默认为当前目录下的 webpage_资源名
            wait_for_load (bool): 是否等待页面完全加载,默认 True
            headless (bool): 是否使用无头模式,默认 True            
        返回:
            bool: 保存成功返回 True,失败返回 False
        """
        # 默认输出目录
        if output_dir is None:
            # 该操作仅为单篇文章保存做测试
            # 输出目录固定为: all_data/wechat_article_full
            output_dir = "all_data/wechat_article_full"
        
        # 创建目录结构
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        # 创建资源目录
        resources_dir = output_path / "resources"
        resources_dir.mkdir(exist_ok=True)
        
        images_dir = resources_dir / "images"
        css_dir = resources_dir / "css"
        js_dir = resources_dir / "js"
        fonts_dir = resources_dir / "fonts"
        
        images_dir.mkdir(exist_ok=True)
        css_dir.mkdir(exist_ok=True)
        js_dir.mkdir(exist_ok=True)
        fonts_dir.mkdir(exist_ok=True)
        
        try:
            with sync_playwright() as p:
                # 启动浏览器 - 明确不使用持久化上下文，避免创建 false 目录
                browser = p.chromium.launch(
                    headless=headless,
                    # 不设置 user_data_dir，避免错误的目录名
                )
                # 创建新的浏览器上下文（隐身模式，不保存数据）
                context = browser.new_context(
                    viewport={'width': 1920, 'height': 1080},  # 设置视口大小
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                )
                page = context.new_page()
                
                # 用于存储已下载的资源
                downloaded_resources = {}
                
                # 监听所有网络请求
                def handle_response(response):
                    try:
                        resource_url = response.url
                        content_type = response.headers.get('content-type', '')
                        
                        # 判断资源类型
                        if any(t in content_type.lower() for t in ['image/', 'font/']):
                            # 下载资源
                            resource_data = response.body()
                            if resource_data:
                                # 生成本地文件名
                                local_path = self._save_resource(
                                    resource_url, 
                                    resource_data, 
                                    content_type,
                                    images_dir if 'image' in content_type else fonts_dir
                                )
                                if local_path:
                                    # 计算相对路径
                                    rel_path = os.path.relpath(local_path, output_path)
                                    downloaded_resources[resource_url] = rel_path.replace('\\', '/')
                    except Exception as e:
                        print(f"处理资源时出错 {response.url}: {str(e)}")
                
                page.on('response', handle_response)
                
                # 访问页面
                print(f"\n正在访问: {url}")
                page.goto(url, wait_until="networkidle")
                
                # 获取页面标题
                title = page.title()
                print(f"页面标题: {title}")
                
                # 第一次等待,确保初始内容加载
                print("等待页面初始加载...")
                page.wait_for_timeout(500)      # 等待0.5秒
                
                # 如果需要滚动页面来触发懒加载
                if self.scroll_to_bottom:
                    print("正在滚动页面以触发懒加载...")
                    self._scroll_page_to_load_all_content(page, self.scroll_delay)
                    print("滚动完成")
                    
                    # 等待网络空闲
                    print("等待网络请求完成...")
                    page.wait_for_load_state("networkidle", timeout=10000)
                
                # 检测 DOM 稳定性
                print("检测页面稳定性...")
                self._wait_for_dom_stable(page)
                print("页面已完全渲染")
                
                # 最后再等待一下确保所有资源都已下载
                page.wait_for_timeout(500)
                
                # ===== 关键步骤:强制将懒加载图片的URL写入DOM =====
                # 很多网站使用 data-src 等属性实现懒加载,但这些URL不在HTML的src中
                # 我们需要在获取HTML之前,用JavaScript将懒加载URL复制到src属性
                print("正在强制渲染懒加载图片到DOM...")
                page.evaluate("""
                () => {
                    const images = document.querySelectorAll('img');
                    let updated = 0;
                    
                    images.forEach(img => {
                        // 常见的懒加载属性列表
                        const lazyAttrs = [
                            'data-src', 
                            'data-original', 
                            'data-original-src',
                            'data-lazy-src', 
                            'data-actualsrc',
                            'data-echo'
                        ];
                        
                        // 尝试从懒加载属性中获取真实URL
                        for (const attr of lazyAttrs) {
                            const value = img.getAttribute(attr);
                            if (value && value.trim() && !value.startsWith('data:image')) {
                                // 如果当前src为空或是占位符,用懒加载URL替换
                                const currentSrc = img.src || '';
                                if (!currentSrc || 
                                    currentSrc.includes('blank') || 
                                    currentSrc.startsWith('data:image')) {
                                    img.src = value;
                                    updated++;
                                    break;
                                }
                            }
                        }
                    });
                    
                    console.log('强制渲染了 ' + updated + ' 个懒加载图片');
                    return updated;
                }
                """)
                
                # 等待一下让新的URL生效并触发下载
                page.wait_for_timeout(1000)
                print("懒加载图片已渲染到DOM")
                
                # 获取 HTML 内容
                html_content = page.content()
                
                # 下载 CSS 和其中的资源
                print("正在下载 CSS 文件...")
                html_content = self._download_css_and_update_html(
                    html_content, page, css_dir, output_path, downloaded_resources
                )
                
                # 下载 JS 文件
                print("正在下载 JS 文件...")
                html_content = self._download_js_and_update_html(
                    html_content, page, js_dir, output_path
                )
                
                # 替换 HTML 中的资源链接
                print("正在更新 HTML 中的资源链接...")
                for original_url, local_path in downloaded_resources.items():
                    # 处理各种可能的 URL 格式
                    patterns = [
                        f'src="{re.escape(original_url)}"',
                        f"src='{re.escape(original_url)}'",
                        f'href="{re.escape(original_url)}"',
                        f"href='{re.escape(original_url)}'",
                        f'url({re.escape(original_url)})',
                        f"url('{re.escape(original_url)}')",
                        f'url("{re.escape(original_url)}")',
                    ]
                    
                    for pattern in patterns:
                        if 'src=' in pattern:
                            replacement = f'src="{local_path}"'
                        elif 'href=' in pattern:
                            replacement = f'href="{local_path}"'
                        else:
                            replacement = f'url({local_path})'
                        
                        html_content = html_content.replace(pattern.replace('\\', ''), replacement)
                
                # 保存 HTML 文件
                html_path = output_path / self.html_filename
                with open(html_path, 'w', encoding='utf-8') as f:
                    f.write(html_content)
                
                print(f"\n✅ 网页保存成功!")
                print(f"📁 保存位置: {output_path.absolute()}")
                print(f"📄 HTML 文件: {html_path.absolute()}")
                print(f"📦 资源文件: {len(downloaded_resources)} 个")
                
                # 关闭浏览器
                browser.close()
                
                return True
                
        except Exception as e:
            print(f"保存网页时出错: {str(e)}")
            import traceback
            traceback.print_exc()
            return False


    def _scroll_page_to_load_all_content(self, page, scroll_delay: int = 300):
        """滚动页面到底部以触发懒加载内容"""
        previous_height = page.evaluate("document.body.scrollHeight")
        scroll_count = 0
        max_scrolls = 50  # 最大滚动次数,避免无限循环
        stable_count = 0  # 高度稳定计数
        
        while scroll_count < max_scrolls:
            # 滚动到页面底部
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            scroll_count += 1
            
            # 等待一段时间让内容加载
            page.wait_for_timeout(scroll_delay)
            
            # 获取新的页面高度
            new_height = page.evaluate("document.body.scrollHeight")
            
            # 如果页面高度没有变化
            if new_height == previous_height:
                stable_count += 1
                # 连续 3 次高度不变,认为已到底部
                if stable_count >= 3:
                    print(f"  滚动完成,共滚动 {scroll_count} 次")
                    break
            else:
                stable_count = 0  # 重置稳定计数
                
            previous_height = new_height
        
        # 滚动回顶部
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(200)


    def _wait_for_dom_stable(self, page, max_wait_seconds: int = 5):
        """
        等待 DOM 稳定(不再有新的元素添加)
        
        参数:
            page: Playwright 页面对象
            max_wait_seconds: 最大等待时间(秒)
        """
        script = """
        (maxWaitMs) => {
            return new Promise((resolve) => {
                let lastCount = document.getElementsByTagName('*').length;
                let stableCount = 0;
                const requiredStableCount = 3;
                const checkInterval = 500; // 每 500ms 检查一次
                let totalWaitTime = 0;
                
                const checkStability = setInterval(() => {
                    const currentCount = document.getElementsByTagName('*').length;
                    totalWaitTime += checkInterval;
                    
                    if (currentCount === lastCount) {
                        stableCount++;
                        if (stableCount >= requiredStableCount || totalWaitTime >= maxWaitMs) {
                            clearInterval(checkStability);
                            resolve(true);
                        }
                    } else {
                        stableCount = 0;
                        lastCount = currentCount;
                    }
                }, checkInterval);
            });
        }
        """
        
        try:
            page.evaluate(script, max_wait_seconds * 1000)
        except Exception as e:
            print(f"  DOM 稳定性检测超时: {str(e)}")


    def _save_resource(self, url: str, data: bytes, content_type: str, target_dir: Path) -> Path:
        """保存资源文件到本地"""
        try:
            # 从 URL 中提取文件扩展名
            parsed_url = urlparse(url)
            path = parsed_url.path
            
            # 获取文件扩展名
            ext = os.path.splitext(path)[1]
            if not ext:
                # 根据 content-type 推断扩展名
                ext_map = {
                    'image/jpeg': '.jpg',
                    'image/jpg': '.jpg',
                    'image/png': '.png',
                    'image/gif': '.gif',
                    'image/webp': '.webp',
                    'image/svg+xml': '.svg',
                    'font/woff': '.woff',
                    'font/woff2': '.woff2',
                    'font/ttf': '.ttf',
                    'font/otf': '.otf',
                }
                for ct, e in ext_map.items():
                    if ct in content_type.lower():
                        ext = e
                        break
            
            # 使用 URL 的哈希值作为文件名,避免重复和特殊字符问题
            url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
            filename = f"{url_hash}{ext}"
            
            file_path = target_dir / filename
            
            # 保存文件
            with open(file_path, 'wb') as f:
                f.write(data)
            
            return file_path
            
        except Exception as e:
            print(f"保存资源失败 {url}: {str(e)}")
            return None


    def _download_css_and_update_html(self, html: str, page, css_dir: Path, output_path: Path, downloaded_resources: dict) -> str:
        """下载 CSS 文件并更新 HTML"""
        # 查找所有 <link rel="stylesheet"> 标签
        link_pattern = r'<link[^>]*rel=["\']stylesheet["\'][^>]*href=["\']([^"\']+)["\'][^>]*>'
        
        matches = re.finditer(link_pattern, html, re.IGNORECASE)
        
        for match in matches:
            css_url = match.group(1)
            
            # 跳过已处理的和 data: URL
            if css_url.startswith('data:') or css_url in downloaded_resources:
                continue
            
            try:
                # 获取绝对 URL
                absolute_url = urljoin(page.url, css_url)
                
                # 下载 CSS
                response = page.request.get(absolute_url)
                if response.ok:
                    css_content = response.body()
                    
                    # 生成本地文件名
                    url_hash = hashlib.md5(absolute_url.encode()).hexdigest()[:12]
                    filename = f"{url_hash}.css"
                    file_path = css_dir / filename
                    
                    # 保存 CSS 文件
                    with open(file_path, 'wb') as f:
                        f.write(css_content)
                    
                    # 计算相对路径
                    rel_path = os.path.relpath(file_path, output_path).replace('\\', '/')
                    downloaded_resources[css_url] = rel_path
                    downloaded_resources[absolute_url] = rel_path
                    
            except Exception as e:
                print(f"下载 CSS 失败 {css_url}: {str(e)}")
        
        return html


    def _download_js_and_update_html(self, html: str, page, js_dir: Path, output_path: Path) -> str:
        """下载 JS 文件并更新 HTML"""
        # 查找所有 <script src="..."> 标签
        script_pattern = r'<script[^>]*src=["\']([^"\']+)["\'][^>]*>'
        
        matches = re.finditer(script_pattern, html, re.IGNORECASE)
        downloaded_js = {}
        
        for match in matches:
            js_url = match.group(1)
            
            # 跳过已处理的和 data: URL
            if js_url.startswith('data:') or js_url in downloaded_js:
                continue
            
            try:
                # 获取绝对 URL
                absolute_url = urljoin(page.url, js_url)
                
                # 下载 JS
                response = page.request.get(absolute_url)
                if response.ok:
                    js_content = response.body()
                    
                    # 生成本地文件名
                    url_hash = hashlib.md5(absolute_url.encode()).hexdigest()[:12]
                    filename = f"{url_hash}.js"
                    file_path = js_dir / filename
                    
                    # 保存 JS 文件
                    with open(file_path, 'wb') as f:
                        f.write(js_content)
                    
                    # 计算相对路径
                    rel_path = os.path.relpath(file_path, output_path).replace('\\', '/')
                    downloaded_js[js_url] = rel_path
                    downloaded_js[absolute_url] = rel_path
                    
                    # 替换 HTML 中的引用
                    html = html.replace(f'src="{js_url}"', f'src="{rel_path}"')
                    html = html.replace(f"src='{js_url}'", f"src='{rel_path}'")
                    
            except Exception as e:
                print(f"下载 JS 失败 {js_url}: {str(e)}")
        
        return html


# 主程序示例
if __name__ == "__main__":
    # 测试 URL
    url = "https://mp.weixin.qq.com/s/qkRJ_UEX2Iv4vkcsx3OyoQ"
    output_dir = "./all_data/wechat_article_full"   # 输出目录
    

    # save_to_html = SaveWebpageToHtml()
    # html_filename = "index.html"                    # HTML 文件名
    # save_to_html.html_filename = html_filename      # 可使用默认值
    # save_to_html.save_webpage_with_resources(url, output_dir)

    # 简便方式
    ss = SaveWebpageToHtml().save_webpage_with_resources(url, output_dir)
    print(ss)
