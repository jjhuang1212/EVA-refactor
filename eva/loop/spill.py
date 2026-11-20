"""大工具输出溢出：模型视图里只留首尾预览和取回方式，完整文本存对象存储。"""


def preview(data: bytes, key: str, edge_bytes: int) -> str:
    """data 是完整文本的 UTF-8 字节。首尾各取 edge_bytes（在字符边界上收尾），中间换成说明。"""
    head = data[:edge_bytes].decode("utf-8", "ignore")
    tail = data[-edge_bytes:].decode("utf-8", "ignore")
    omitted = len(data) - 2 * edge_bytes
    return (f"{head}\n…（中间省略约 {omitted} 字节；完整内容已存为 overflow_key={key}，"
            f"用 read_file 的 overflow_key 参数分页读取）…\n{tail}")
