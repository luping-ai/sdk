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
	// Stale 是失效向量数（换了 Embedding 模型）
	Stale int `json:"stale_chunks"`
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
