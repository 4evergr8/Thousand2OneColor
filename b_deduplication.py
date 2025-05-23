import os
import shutil
import numpy as np
from PIL import Image
import imagehash
from tqdm import tqdm

# ===== 参数配置 =====
HASH_DIFF_THRESHOLD = 6

IMG_SUFFIXES = ('.jpg', '.jpeg', '.png', '.bmp', '.webp')

# ===== 路径配置 =====
base_dir = os.path.dirname(os.path.abspath(__file__))
train_dir = os.path.join(base_dir, 'train')
delete_dir = os.path.join(train_dir, 'delete')
os.makedirs(delete_dir, exist_ok=True)


def move_to_delete(src_path):
    rel_path = os.path.relpath(src_path, train_dir)
    dst_path = os.path.join(delete_dir, rel_path)
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    shutil.move(src_path, dst_path)
    print(f"已移动到：{dst_path}")


# ===== 第零部分：删除宽或高小于512的图像 =====
print("===== 第零部分：删除尺寸过小图像（宽或高 < 512） =====")
for root, dirs, _ in os.walk(train_dir):
    if delete_dir in root:
        continue
    for subdir in dirs:
        folder = os.path.join(root, subdir)
        for file in os.listdir(folder):
            if not file.lower().endswith(IMG_SUFFIXES):
                continue
            path = os.path.join(folder, file)
            try:
                with Image.open(path) as img:
                    w, h = img.size
                    if w < 512 or h < 512:
                        print(f"{path} - 尺寸：{w}x{h}，已删除")
                        img.close()
                        move_to_delete(path)
            except Exception as e:
                print(f"处理失败（尺寸判断）：{path}，错误：{e}")

# ===== 第一部分：删除色彩单调图像 =====
print("\n===== 第一部分：删除色彩单调图像（按色彩饱和度） =====")
for root, dirs, _ in os.walk(train_dir):
    if delete_dir in root:
        continue
    for subdir in dirs:
        folder = os.path.join(root, subdir)
        for file in os.listdir(folder):
            if not file.lower().endswith(IMG_SUFFIXES):
                continue
            path = os.path.join(folder, file)
            try:
                with Image.open(path) as img:
                    if img.mode != 'RGB':
                        img = img.convert('RGB')
                    img_hsv = img.convert('HSV')
                    hsv = np.array(img_hsv)
                    saturation = hsv[..., 1]
                    mean_sat = saturation.mean()

                    if mean_sat < 30:
                        print(f"{path} - 平均饱和度: {mean_sat:.2f}（低于{40}，已删除）")
                        move_to_delete(path)

                    elif mean_sat > 100:
                        std_sat = saturation.std()
                        print(f"{path} - 平均饱和度: {mean_sat:.2f}，标准差: {std_sat:.2f}")
                        if std_sat < 60:
                            print(f"{path} - 标准差低于 {60}，已删除")
                            move_to_delete(path)
                        else:
                            print(f"{path} - 标准差高于 {60}，已保留")
                    elif mean_sat > 55:
                        std_sat = saturation.std()
                        print(f"{path} - 平均饱和度: {mean_sat:.2f}，标准差: {std_sat:.2f}")
                        if std_sat < 30:
                            print(f"{path} - 标准差低于 {30}，已删除")
                            move_to_delete(path)
                        else:
                            print(f"{path} - 标准差高于 {30}，已保留")
                    elif mean_sat > 30:
                        std_sat = saturation.std()
                        print(f"{path} - 平均饱和度: {mean_sat:.2f}，标准差: {std_sat:.2f}")
                        if std_sat < 20:
                            print(f"{path} - 标准差低于 {20}，已删除")
                            move_to_delete(path)
                        else:
                            print(f"{path} - 标准差高于 {20}，已保留")
                    else:
                        print(
                            f"{path} - 平均饱和度: {mean_sat:.2f}，已保留）")


            except Exception as e:
                print(f"处理失败（色彩饱和度判断）：{path}，错误：{e}")

# ===== 第二部分：使用哈希去重保留唯一图像 =====
print("\n===== 第二部分：使用哈希去重图像 =====")
hash_func = imagehash.phash

for root, dirs, _ in os.walk(train_dir):
    if delete_dir in root:
        continue
    for subdir in dirs:
        subfolder = os.path.join(root, subdir)
        hash_dict = {}
        visited = set()
        similar_groups = []

        print(f"\n处理子文件夹：{subfolder}")
        image_files = [f for f in os.listdir(subfolder) if f.lower().endswith(IMG_SUFFIXES)]

        print("计算图片哈希中...")
        for filename in tqdm(image_files):
            filepath = os.path.join(subfolder, filename)
            try:
                with Image.open(filepath) as img:
                    h = hash_func(img)
                    hash_dict[filepath] = h
            except Exception:
                continue

        filepaths = list(hash_dict.keys())
        print("查找相似图片中...")
        for i in tqdm(range(len(filepaths))):
            if filepaths[i] in visited:
                continue
            group = [filepaths[i]]
            visited.add(filepaths[i])
            for j in range(i + 1, len(filepaths)):
                if filepaths[j] in visited:
                    continue
                if hash_dict[filepaths[i]] - hash_dict[filepaths[j]] <= HASH_DIFF_THRESHOLD:
                    group.append(filepaths[j])
                    visited.add(filepaths[j])
            if len(group) > 1:
                similar_groups.append(group)

        print(f"发现 {len(similar_groups)} 组相似图片，开始移动多余文件...")
        for group in similar_groups:
            for filepath in group[1:]:
                try:
                    move_to_delete(filepath)
                except Exception:
                    continue

        print("该子文件夹处理完成。")

print("\n所有处理完成。")
