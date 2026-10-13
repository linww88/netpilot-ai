"""日志配置""" 
import logging 
import os
import sys 

def setup_logger(level=logging.INFO): 
    """配置根日志记录器""" 
    # 确保日志目录存在（全新克隆的仓库不会有 data/ 目录，
    # 若此处不自动创建，FileHandler 会直接抛 FileNotFoundError 导致应用无法启动）
    log_dir = os.path.join(os.getcwd(), "data")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "app.log")
    logging.basicConfig( 
        level=level, 
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
        handlers=[ 
            logging.StreamHandler(sys.stdout), 
            logging.FileHandler(log_file, encoding='utf-8') 
        ] 
    )
