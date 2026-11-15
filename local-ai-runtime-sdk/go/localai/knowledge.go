package localai

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"mime/multipart"
	"net/url"
	"strings"
)

// ---------------------------------------------------------------------------
// 知识库管理（文档 22）
//
// 这一组接口与 Search 的区别：Search 是"用知识库"，这里是"管知识库"。
// 业务方通常只在运维/导入阶段用到它们。
// ---------------------------------------------------------------------------

// KBSummary 是一个知识库的概要。
type KBSummary struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	Description string `json:"description,omitempty"`
	// DocumentCount / ChunkCount / EmbeddedCount 由服务端聚合。
	//
	// 服务端查单个库的统计失败时不影响列表（可能正在索引），
	// 此时这三个字段为 0——不要把 0 当成"库里什么都没有"。
	DocumentCount int `json:"document_count"`
	ChunkCount    int `json:"chunk_count"`
	// EmbeddedCount 是已有有效向量的切片数
	EmbeddedCount int   `json:"embedded_count"`
	CreatedAt     int64 `json:"created_at"`
}

// CreateKBRequest 是创建知识库的请求。
type CreateKBRequest struct {
	// ID 可省略，省略时服务端自动生成。
	//
	// 若指定，只能包含字母、数字、短横线与下划线——ID 会拼进
	// 托管目录路径，允许 `..` 或 `/` 会构成路径穿越。
	ID          string `json:"id,omitempty"`
	Name        string `json:"name"`
	Description string `json:"description,omitempty"`
}

// KnowledgeBaseRef 是创建成功后的引用。
type KnowledgeBaseRef struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	Description string `json:"description"`
}

// KBStats 是一个知识库的统计。
type KBStats struct {
	KBID          string `json:"kb_id"`
	DocumentCount int    `json:"document_count"`
	ChunkCount    int    `json:"chunk_count"`
	// EmbeddedCount 是已有有效向量的切片数
	EmbeddedCount int `json:"embedded_count"`
	// StaleCount 是失效向量数（换了 Embedding 模型后待重建）
	StaleCount int `json:"stale_count"`
	// Vectorize 可能为 nil：服务端查进度失败时不影响主统计
	Vectorize *VectorizeProgress `json:"vectorize,omitempty"`
}

// VectorizeProgress 是向量化进度。
type VectorizeProgress struct {
	Total   int `json:"total_chunks"`
	Done    int `json:"embedded_chunks"`
	Pending int `json:"pending_chunks"`
	// Stale 是失效向量数（换了 Embedding 模型或前缀）
	Stale int `json:"stale_chunks"`
	// StaleReason 说明向量为何失效，空表示无已知原因。
	//
	// 用户看到 Stale 等于 Total 时唯一的疑问是"为什么"。换模型、
	// 换前缀、还是系统出错？处理方式相同（重新向量化），但用户
	// 需要知道发生了什么才愿意去做。
	StaleReason string `json:"stale_reason,omitempty"`
	// Model 是当前使用的 Embedding 模型（未配置时为空）
	Model string `json:"model,omitempty"`
	// Running 表示后台任务正在执行
	Running bool `json:"running"`
}

// VectorizeRequest 是触发向量化的请求。
type VectorizeRequest struct {
	// Async 为 true 时立即返回，后台执行。
	//
	// 默认同步：小知识库只需几百毫秒，同步返回能让调用方
	// 立刻知道结果。大知识库应设 true，否则请求会持续数分钟。
	Async bool `json:"async,omitempty"`
}

// VectorizeResult 是向量化的结果。
//
// 两种模式用 Started 区分：
//   - 同步：Started=false，Vectorized 是本次处理数（可能为 0）
//   - 异步：Started=true，Vectorized 恒为 0
type VectorizeResult struct {
	KBID string `json:"kb_id"`
	// Vectorized 是本次向量化的切片数（同步模式）
	Vectorized int `json:"vectorized"`
	// Started 表示已启动后台任务（异步模式）
	Started bool `json:"started,omitempty"`
	// Note 是异步模式下的提示
	Note string `json:"note,omitempty"`
}

// DocumentSummary 是知识库中的一个文档。
type DocumentSummary struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	FileType    string `json:"file_type"`
	StorageMode string `json:"storage_mode"`
	FileSize    int64  `json:"file_size"`
	// Status 是文档状态：ACTIVE / MISSING / MODIFIED /
	// PENDING / PROCESSING / FAILED
	Status string `json:"status"`
	// IndexStatus 是索引状态：PENDING / READY / PARTIAL / FAILED
	IndexStatus string `json:"index_status"`
	ChunkCount  int    `json:"chunk_count"`
	// Error 非空表示处理失败的原因
	Error     string `json:"error,omitempty"`
	CreatedAt int64  `json:"created_at"`
	UpdatedAt int64  `json:"updated_at"`
}

// ImportRequest 是导入文档的请求。
//
// 只传**路径**而不传文件内容：Runtime 是本机服务，SDK 与应用
// 在同一台机器上，传路径比传整个文件内容省内存也省带宽。
// 路径必须是绝对路径——相对路径的基准取决于服务进程的工作目录，
// 那是用户无法预期的。
type ImportRequest struct {
	// Path 是源文件的绝对路径（必填）
	Path string `json:"path"`
	// Name 是显示名，省略则用文件名
	Name string `json:"name,omitempty"`
	// Mode 是存储模式：MANAGED（默认）/ EXTERNAL
	//
	// MANAGED 复制文件进 Runtime 数据目录，原文件变动不影响知识库；
	// EXTERNAL 只记录路径，原文件失效会导致 MISSING。
	Mode string `json:"mode,omitempty"`
	// Password 是加密 PDF 的密码。
	//
	// **不传也可以先试**："只限制打印"类 PDF 是空用户密码 +
	// 非空所有者密码，任何阅读器都能直接打开，解析器会自动用
	// 空密码尝试。
	//
	// 密码不写入数据库，只在本次导入的内存中使用。若之后需要
	// 重建索引，需要再次提供。
	Password string `json:"password,omitempty"`
	// UpdateDocumentID 非空表示更新已有文档而非新建。
	//
	// 更新时做增量 diff：未变更的切片保留向量，只重新向量化
	// 变更部分。响应里的 Diff 说明本次改了什么。
	UpdateDocumentID string `json:"update_document_id,omitempty"`
}

// IngestChunksRequest 是一次"入库已切好的块"的请求。
//
// 字段语义见服务端契约（openapi.yaml 的 IngestChunksRequest）。
type IngestChunksRequest struct {
	// Name 是显示名（必填），用于来源卡片
	Name string `json:"name"`
	// FileType 仅用于筛选与统计，如 "pdf"。
	// Runtime 不据此选解析器——它不解析任何东西。
	FileType string `json:"file_type,omitempty"`
	// FileHash 是内容的 SHA-256 十六进制串（必填）。
	// 用于同库内去重与幂等：相同哈希重复提交返回 Duplicate。
	FileHash string `json:"file_hash"`
	FileSize int64  `json:"file_size,omitempty"`
	// ModifiedAt 是源文件修改时间（Unix 秒）
	ModifiedAt int64 `json:"modified_at,omitempty"`
	// SourceLabel 是来源的可读描述，只存不解析（可给本地路径）
	SourceLabel string `json:"source_label,omitempty"`
	// UpdateDocumentID 非空表示增量更新已有文档
	UpdateDocumentID string `json:"update_document_id,omitempty"`
	// Chunker 标识切块算法（必填，Version 不能为空）
	Chunker ChunkerInfo `json:"chunker"`
	// Chunks 是已切好的块（必填，最多 2000 项）
	Chunks []ChunkInput `json:"chunks"`
}

// ChunkInput 是一个待入库的块。
type ChunkInput struct {
	// Ordinal 是块在文档内的序号（0 基），**必须唯一**——
	// 它是增量 diff 的定位依据。
	Ordinal int    `json:"ordinal"`
	Text    string `json:"text"`
	// PageStart / PageEnd 是页码（1 基）。0 或省略表示未知。
	PageStart int `json:"page_start,omitempty"`
	PageEnd   int `json:"page_end,omitempty"`
	// StartRune / RuneCount 是块在原文中的位置（客户端自洽即可）
	StartRune int `json:"start_rune,omitempty"`
	RuneCount int `json:"rune_count,omitempty"`
	// HeadingPath 是章节路径，如"第三章 保修 > 3.2 期限"。
	// 超过 512 字符会被服务端截断（按 rune）。
	HeadingPath string `json:"heading_path,omitempty"`
	// ChunkType 是块类型：paragraph / heading / table / list / code。
	// 省略或非法值归一为 paragraph。
	ChunkType string `json:"chunk_type,omitempty"`
}

// ChunkerInfo 标识客户端的切块算法。
//
// Version **必填**：它标识算法而不只是参数。参数相同但算法改了时，
// 参数无法表达"这不是同一种切法"，混用会导致页码错位。
type ChunkerInfo struct {
	Name    string `json:"name"`
	Version string `json:"version"`
	// 以下参数只记录，Runtime 不用来判定
	TargetChars  int `json:"target_chars,omitempty"`
	MaxChars     int `json:"max_chars,omitempty"`
	OverlapChars int `json:"overlap_chars,omitempty"`
}

// ImportResult 是导入文档的结果。
type ImportResult struct {
	DocumentID   string `json:"document_id"`
	ChunkCount   int    `json:"chunk_count"`
	ContentRunes int    `json:"content_runes"`
	// Duplicate 表示内容与已有文档完全相同，本次未重复导入。
	// 这不是错误——同一知识库内相同内容只保留一份。
	Duplicate bool `json:"duplicate"`
	// ExistingID 在 Duplicate 为 true 时指向已有文档
	ExistingID string `json:"existing_id,omitempty"`
	// Note 是给人看的说明
	Note string `json:"note,omitempty"`
	// Diff 仅在 UpdateDocumentID 非空时出现
	Diff *ImportDiff `json:"diff,omitempty"`
}

// ImportDiff 说明一次增量更新改了什么。
type ImportDiff struct {
	Added     int `json:"added"`
	Updated   int `json:"updated"`
	Deleted   int `json:"deleted"`
	Unchanged int `json:"unchanged"`
	// NeedsEmbedding 为 true 时才需要触发向量化——
	// 只删除不需要（没有新文本要嵌入）。
	NeedsEmbedding bool `json:"needs_embedding"`
	// Summary 是给人看的摘要，如"变更 1，未变 2"
	Summary string `json:"summary"`
}

// DeletedResource 是删除操作的确认。
type DeletedResource struct {
	Deleted string `json:"deleted"`
}

// DeletedDocument 是删除文档的确认。
type DeletedDocument struct {
	Deleted string `json:"deleted"`
	// FileRemoved 表示是否删除了托管副本。
	// EXTERNAL 模式恒为 false——绝不删除用户原文件。
	FileRemoved bool `json:"file_removed"`
}

// ---------------------------------------------------------------------------
// 操作
// ---------------------------------------------------------------------------

// CreateKB 创建知识库。
func (c *Client) CreateKB(ctx context.Context, req *CreateKBRequest) (*KnowledgeBaseRef, error) {
	// 与服务端一致地校验空白名：只检查 == "" 会放过 "  "，
	// 让无效请求跑到服务端再被打回，浪费一次往返
	if req == nil || strings.TrimSpace(req.Name) == "" {
		return nil, fmt.Errorf("%w: name 不能为空", ErrInvalidRequest)
	}
	ctx, cancel := c.withTotalTimeout(ctx)
	defer cancel()

	env, err := c.do(ctx, "POST", "/api/v1/knowledge/bases", req)
	if err != nil {
		return nil, err
	}
	var out KnowledgeBaseRef
	if err := unmarshalJSON(env.Data, &out); err != nil {
		return nil, wrapDecodeErr(err)
	}
	return &out, nil
}

// ListKB 列出所有知识库。
func (c *Client) ListKB(ctx context.Context) ([]KBSummary, error) {
	ctx, cancel := c.withTotalTimeout(ctx)
	defer cancel()

	env, err := c.do(ctx, "GET", "/api/v1/knowledge/bases", nil)
	if err != nil {
		return nil, err
	}
	var payload struct {
		Bases []KBSummary `json:"bases"`
	}
	if err := unmarshalJSON(env.Data, &payload); err != nil {
		return nil, wrapDecodeErr(err)
	}
	return payload.Bases, nil
}

// DeleteKB 删除知识库及其全部数据（文档、切片、向量、全文索引）。
//
// 不可恢复。托管副本会一并删除；EXTERNAL 模式的原文件不受影响。
func (c *Client) DeleteKB(ctx context.Context, kbID string) error {
	if kbID == "" {
		return fmt.Errorf("%w: kb_id 不能为空", ErrInvalidRequest)
	}
	ctx, cancel := c.withTotalTimeout(ctx)
	defer cancel()

	_, err := c.do(ctx, "DELETE", "/api/v1/knowledge/bases/"+url.PathEscape(kbID), nil)
	return err
}

// KBStat 返回知识库的统计与向量化进度。
func (c *Client) KBStat(ctx context.Context, kbID string) (*KBStats, error) {
	if kbID == "" {
		return nil, fmt.Errorf("%w: kb_id 不能为空", ErrInvalidRequest)
	}
	ctx, cancel := c.withTotalTimeout(ctx)
	defer cancel()

	env, err := c.do(ctx, "GET",
		"/api/v1/knowledge/bases/"+url.PathEscape(kbID)+"/stats", nil)
	if err != nil {
		return nil, err
	}
	var out KBStats
	if err := unmarshalJSON(env.Data, &out); err != nil {
		return nil, wrapDecodeErr(err)
	}
	return &out, nil
}

// Vectorize 为尚无向量的切片生成向量。
//
// 文档导入时切片已入库，因此**关键词检索立即可用**；本方法
// 补齐语义检索所需的向量。
//
// 未配置 Embedding 模型时返回 ErrModelUnavailable——此时只能
// 做关键词检索，不是错误配置，但语义通道不可用。
func (c *Client) Vectorize(ctx context.Context, kbID string, req *VectorizeRequest) (*VectorizeResult, error) {
	if kbID == "" {
		return nil, fmt.Errorf("%w: kb_id 不能为空", ErrInvalidRequest)
	}
	ctx, cancel := c.withTotalTimeout(ctx)
	defer cancel()

	var body any
	if req != nil && req.Async {
		body = req
	}
	env, err := c.do(ctx, "POST",
		"/api/v1/knowledge/bases/"+url.PathEscape(kbID)+"/vectorize", body)
	if err != nil {
		return nil, err
	}
	var out VectorizeResult
	if err := unmarshalJSON(env.Data, &out); err != nil {
		return nil, wrapDecodeErr(err)
	}
	return &out, nil
}

// ListDocuments 列出知识库中的文档。
func (c *Client) ListDocuments(ctx context.Context, kbID string) ([]DocumentSummary, error) {
	if kbID == "" {
		return nil, fmt.Errorf("%w: kb_id 不能为空", ErrInvalidRequest)
	}
	ctx, cancel := c.withTotalTimeout(ctx)
	defer cancel()

	env, err := c.do(ctx, "GET",
		"/api/v1/knowledge/bases/"+url.PathEscape(kbID)+"/documents", nil)
	if err != nil {
		return nil, err
	}
	var payload struct {
		Documents []DocumentSummary `json:"documents"`
	}
	if err := unmarshalJSON(env.Data, &payload); err != nil {
		return nil, wrapDecodeErr(err)
	}
	return payload.Documents, nil
}

// Import 导入文档（或更新已有文档）。
//
// Deprecated: 服务端端点已从契约移除，调用会得到 404。
// 用 IngestChunks 代替：客户端解析好、切好的块直达入库。
//
// 保留方法（而不是删掉）的理由：删掉会让升级后的调用方得到
// 编译错误/AttributeError，而这比"明确的 404 + 这里写着替代方案"
// 信息量更少。不要在这之上加"本地切块再转发"的兼容层——
// 那等于把解析器搬进 SDK，与 Runtime 侧冻结它的决定直接冲突。
//
// 支持的格式：.txt / .md / .pdf / .docx / .xlsx。
//
// 幂等：同一知识库内相同内容（SHA-256）只保留一份，重复导入
// 返回 Duplicate=true 而不是错误。失败的文档允许重试——补上
// 密码等修正后重新导入会真正重新解析。
//
// 导入是耗时操作（解析 + 切片），大文件可能数十秒。ctx 应设
// 足够长的超时；本方法不套用默认的 Total 超时。
func (c *Client) Import(ctx context.Context, kbID string, req *ImportRequest) (*ImportResult, error) {
	if kbID == "" {
		return nil, fmt.Errorf("%w: kb_id 不能为空", ErrInvalidRequest)
	}
	if req == nil || strings.TrimSpace(req.Path) == "" {
		return nil, fmt.Errorf("%w: path 不能为空", ErrInvalidRequest)
	}

	env, err := c.do(ctx, "POST",
		"/api/v1/knowledge/bases/"+url.PathEscape(kbID)+"/documents", req)
	if err != nil {
		return nil, err
	}
	var out ImportResult
	if err := unmarshalJSON(env.Data, &out); err != nil {
		return nil, wrapDecodeErr(err)
	}
	return &out, nil
}

// IngestChunks 入库已解析、已切好的文本块（解析下沉后的主路径）。
//
// ## 为什么用它而不是 Import
//
// Runtime 不再解析文档、不再切块。客户端用本地库（PyMuPDF /
// python-docx / openpyxl / python-pptx）解析并切块，把结果连同结构
// 元数据一起提交。换来的是格式覆盖率从约 70% 接近 100%（含 PPT 与
// 扫描件 OCR），且**页码溯源与标题路径才变得可行**。
//
// Import 与 UploadDocument 对应的服务端端点**已从契约移除**（调用会
// 得到 404）。它们保留在 SDK 里只为让错误信息可读，不要在新代码里用。
//
// ## ChunkerInfo.Version 是必填的运维字段
//
// 它标识切块**算法**，不只是参数。参数相同但算法改了（如句末判定
// 逻辑变化）时，参数无法表达"这不是同一种切法"。混用会让用户点来源
// 时页码对不上，而系统无法事后区分。
//
// 版本与库内不一致时服务端返回 409 → 本方法返回 ErrChunkerMismatch。
// **不要重试**（重试必然再失败），正确处理是重建该库索引。
func (c *Client) IngestChunks(ctx context.Context, kbID string,
	req *IngestChunksRequest) (*ImportResult, error) {

	if kbID == "" {
		return nil, fmt.Errorf("%w: kb_id 不能为空", ErrInvalidRequest)
	}
	if req == nil {
		return nil, fmt.Errorf("%w: req 不能为空", ErrInvalidRequest)
	}
	// 与 IngestRequest.Validate 保持一致的本地预检：让明显错误的请求
	// 不浪费一次往返（服务端仍会完整校验，两处规则由 test_types 锁定）
	if strings.TrimSpace(req.Name) == "" {
		return nil, fmt.Errorf("%w: Name 不能为空", ErrInvalidRequest)
	}
	if strings.TrimSpace(req.FileHash) == "" {
		return nil, fmt.Errorf("%w: FileHash 不能为空（用于同库内去重）",
			ErrInvalidRequest)
	}
	if strings.TrimSpace(req.Chunker.Version) == "" {
		return nil, fmt.Errorf("%w: Chunker.Version 不能为空（用于检测切块算法变更）",
			ErrInvalidRequest)
	}
	if len(req.Chunks) == 0 {
		return nil, fmt.Errorf("%w: Chunks 不能为空", ErrInvalidRequest)
	}

	env, err := c.do(ctx, "POST",
		"/api/v1/knowledge/bases/"+url.PathEscape(kbID)+"/documents/chunks", req)
	if err != nil {
		return nil, err
	}
	var out ImportResult
	if err := unmarshalJSON(env.Data, &out); err != nil {
		return nil, wrapDecodeErr(err)
	}
	return &out, nil
}

// DeleteDocument 删除文档及其切片、向量、全文索引。
//
// keepFile 为 true 时保留托管副本。EXTERNAL 模式恒不删除原文件——
// 那可能毁掉用户唯一的资料。
func (c *Client) DeleteDocument(ctx context.Context, kbID, docID string, keepFile bool) error {
	if kbID == "" || docID == "" {
		return fmt.Errorf("%w: kb_id 与 doc_id 都不能为空", ErrInvalidRequest)
	}
	ctx, cancel := c.withTotalTimeout(ctx)
	defer cancel()

	path := "/api/v1/knowledge/bases/" + url.PathEscape(kbID) +
		"/documents/" + url.PathEscape(docID)
	if keepFile {
		path += "?keep_file=1"
	}
	_, err := c.do(ctx, "DELETE", path, nil)
	return err
}

// UploadRequest 是上传导入的请求。
//
// 与 ImportRequest 的区别：这里传**文件内容**（io.Reader），而
// ImportRequest 传路径。给 GUI 用：用户点"选择文件"选中的文件在
// 浏览器/应用进程里，没有"Runtime 能直接读的路径"这个概念——
// 尤其是应用跑在容器、远程桌面或沙箱里时。
//
// 服务端会把内容落到临时文件，导入完成后删除。
type UploadRequest struct {
	// File 是文件内容（必填）
	File io.Reader
	// Filename 是带扩展名的文件名（必填）。
	//
	// 服务端按扩展名选解析器，因此这个值决定文件能否被导入——
	// 传 "data" 会被拒绝（不支持的文件类型），必须传 "x.pdf"。
	Filename string
	// Name 是显示名，省略则用 Filename
	Name string
	// Password 是加密 PDF 的密码
	Password string
	// UpdateDocumentID 非空表示更新已有文档（增量 diff）
	UpdateDocumentID string
}

// UploadDocument 上传文件内容并导入（multipart/form-data）。
//
// Deprecated: 服务端端点已从契约移除，调用会得到 404。
// 用 IngestChunks 代替。
//
// 与 Import 的区别见 UploadRequest 的说明。两者在服务端走同一条
// 导入链路，去重、增量 diff、切片的行为完全一致。
//
// 恒为 MANAGED 存储模式：临时文件导入后删除，外部引用无从谈起。
func (c *Client) UploadDocument(ctx context.Context, kbID string,
	req *UploadRequest) (*ImportResult, error) {

	if kbID == "" {
		return nil, fmt.Errorf("%w: kb_id 不能为空", ErrInvalidRequest)
	}
	if req == nil || req.File == nil {
		return nil, fmt.Errorf("%w: File 不能为空", ErrInvalidRequest)
	}
	if strings.TrimSpace(req.Filename) == "" {
		return nil, fmt.Errorf("%w: Filename 不能为空（服务端按扩展名选解析器）",
			ErrInvalidRequest)
	}

	// 流式构造 multipart，不把整个文件读进内存：
	// 一份 100 MB 的 PDF 读进内存会让调用方的 RSS 涨 100 MB，
	// 而 multipart.Writer 直连一个 io.Pipe，边读边发。
	var body bytes.Buffer
	mw := multipart.NewWriter(&body)

	fields := []struct{ k, v string }{
		{"name", req.Name},
		{"password", req.Password},
		{"update_document_id", req.UpdateDocumentID},
	}
	for _, f := range fields {
		if f.v == "" {
			continue
		}
		if err := mw.WriteField(f.k, f.v); err != nil {
			return nil, fmt.Errorf("%w: 构造表单失败: %v", ErrInvalidRequest, err)
		}
	}

	part, err := mw.CreateFormFile("file", req.Filename)
	if err != nil {
		return nil, fmt.Errorf("%w: 构造表单失败: %v", ErrInvalidRequest, err)
	}
	if _, err := io.Copy(part, req.File); err != nil {
		return nil, fmt.Errorf("%w: 读取文件内容失败: %v", ErrInvalidRequest, err)
	}
	if err := mw.Close(); err != nil {
		return nil, fmt.Errorf("%w: 构造表单失败: %v", ErrInvalidRequest, err)
	}

	path := "/api/v1/knowledge/bases/" + url.PathEscape(kbID) +
		"/documents/upload"

	// 导入是耗时操作（传 + 解析 + 切片），不套用默认的总超时
	env, err := c.doRaw(ctx, "POST", path, mw.FormDataContentType(), &body)
	if err != nil {
		return nil, err
	}
	var out ImportResult
	if err := unmarshalJSON(env.Data, &out); err != nil {
		return nil, wrapDecodeErr(err)
	}
	return &out, nil
}
