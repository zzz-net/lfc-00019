"""生成样例测试数据 - 创建测试用的假发票文件"""
import os


sample_dir = os.path.join(os.path.dirname(__file__), "sample_invoices")
archived_dir = os.path.join(os.path.dirname(__file__), "archived")
conflict_target = os.path.join(archived_dir, "vat_special")


sample_files = [
    ("2024年1月增值税专用发票_12345.pdf", "vat_special"),
    ("2024年1月增值税专用发票_67890.pdf", "vat_special"),
    ("2024年2月增值税普通发票_ABCDE.pdf", "vat_normal"),
    ("电子发票_20240115_001.pdf", "electronic"),
    ("电子发票_20240116_002.pdf", "electronic"),
    ("出差报销单_202401.xlsx", "reimbursement"),
    ("采购合同_供应商A.docx", "contracts"),
    ("发票照片_餐厅发票.jpg", "images"),
    ("发票扫描件_出租车发票.png", "images"),
    ("未分类的文档.txt", None),
    ("notes.md", None),
]


def create_test_data():
    os.makedirs(sample_dir, exist_ok=True)
    os.makedirs(archived_dir, exist_ok=True)
    os.makedirs(conflict_target, exist_ok=True)

    for fname, _ in sample_files:
        fpath = os.path.join(sample_dir, fname)
        if not os.path.exists(fpath):
            with open(fpath, "wb") as f:
                f.write(f"This is a sample file: {fname}\n".encode("utf-8"))
            print(f"  创建: {fpath}")

    conflict_file = os.path.join(conflict_target, "2024年1月增值税专用发票_12345.pdf")
    if not os.path.exists(conflict_file):
        with open(conflict_file, "wb") as f:
            f.write(b"Pre-existing conflict file for testing\n")
        print(f"  创建冲突文件: {conflict_file}")

    print("\n测试数据创建完成！")
    print(f"  源目录: {sample_dir}")
    print(f"  目标目录: {archived_dir}")


if __name__ == "__main__":
    create_test_data()
