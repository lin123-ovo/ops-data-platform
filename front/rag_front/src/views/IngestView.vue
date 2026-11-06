<script setup lang="ts">
import { t } from '@/utils/locale'
import { Check, Files, Refresh, Upload } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import type { UploadFile } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { notifyError } from '@/api/http'
import { notifyDocumentResult } from '@/utils/documentFeedback'
import { useDocumentStore } from '@/stores/documents'
import { formatDateTime, formatFileSize } from '@/utils/format'

const router = useRouter()
const documentStore = useDocumentStore()

const uploading = ref(false)
const textSubmitting = ref(false)

const uploadForm = reactive({
  knowledge_base: 'default',
  preferred_splitter: '',
  file: null as File | null,
})

const textForm = reactive({
  filename: '',
  knowledge_base: 'default',
  preferred_splitter: '',
  content: '',
})

const splitterOptions = computed(() => documentStore.splitters)
const latestDocuments = computed(() => documentStore.documents.slice(0, 6))

function handleUploadChange(uploadFile: UploadFile) {
  uploadForm.file = uploadFile.raw || null
}

// 跟踪上传主线：此函数 → stores/documents.ts → api/modules/documents.ts → 后端 /documents/upload。
async function submitUploadDocument() {
  if (!uploadForm.file) {
    ElMessage.warning(t('请先选择要上传的文档', 'Choose a document to upload'))
    return
  }

  uploading.value = true
  try {
    const document = await documentStore.createUploadDocument({
      file: uploadForm.file,
      knowledge_base: uploadForm.knowledge_base,
      preferred_splitter: uploadForm.preferred_splitter || null,
    })
    notifyDocumentResult(document.summary, t('文档已上传、解析并写入向量索引', 'Document uploaded, parsed, and indexed'))
    Object.assign(uploadForm, {
      knowledge_base: 'default',
      preferred_splitter: '',
      file: null,
    })
    await router.push({ name: 'chunks', query: { documentId: document.id } })
  } catch (error) {
    notifyError(error, t('上传文档失败', 'Could not upload the document'))
  } finally {
    uploading.value = false
  }
}

async function submitTextDocument() {
  if (!textForm.filename.trim() || !textForm.content.trim()) {
    ElMessage.warning(t('请填写文件名和正文内容', 'Enter a file name and content'))
    return
  }

  textSubmitting.value = true
  try {
    const document = await documentStore.createTextDocument({
      filename: textForm.filename.trim(),
      knowledge_base: textForm.knowledge_base,
      preferred_splitter: textForm.preferred_splitter || null,
      content: textForm.content,
    })
    notifyDocumentResult(document.summary, t('文本已完成入库并建立向量索引', 'Text imported and indexed'))
    Object.assign(textForm, {
      filename: '',
      knowledge_base: 'default',
      preferred_splitter: '',
      content: '',
    })
    await router.push({ name: 'chunks', query: { documentId: document.id } })
  } catch (error) {
    notifyError(error, t('文本入库失败', 'Could not import the text'))
  } finally {
    textSubmitting.value = false
  }
}

onMounted(async () => {
  await documentStore.initialize()
})
</script>

<template>
  <div class="page-grid">
    <section class="panel span-12 toolbar-card">
      <div>
        <div class="panel-kicker">Dedicated Ingestion</div>
        <h2>{{ t('上传文档并解析入向量数据库', 'Import and index documents') }}</h2>
        <p class="page-description">
          {{ t('这个页面只负责“上传 -> 解析 -> 切分 -> 写入向量索引”，完成后可直接跳到 Chunk 管理页查看切分和溯源结果。', 'Upload and index a document, then use Chunks to inspect its excerpts and metadata.') }}
        </p>
      </div>
      <div class="toolbar-actions wrap">
        <el-button :icon="Refresh" @click="documentStore.initialize()">{{ t('刷新数据', 'Refresh data') }}</el-button>
        <el-button :icon="Files" @click="router.push({ name: 'documents' })">{{ t('查看文档列表', 'View documents') }}</el-button>
      </div>
    </section>

    <section class="panel span-6 section-card">
      <div class="section-heading">
        <div>
          <div class="panel-kicker">Upload File</div>
          <h3>{{ t('文件上传入库', 'Import a file') }}</h3>
        </div>
        <el-tag effect="dark">{{ t('推荐主流程', 'Recommended') }}</el-tag>
      </div>

      <el-form label-position="top">
        <div class="form-grid two-columns">
          <el-form-item :label="t('知识库名称', 'Knowledge base')">
            <el-input v-model="uploadForm.knowledge_base" />
          </el-form-item>
          <el-form-item :label="t('切分策略', 'Chunking strategy')">
            <el-select v-model="uploadForm.preferred_splitter" clearable :placeholder="t('自动判断', 'Auto')">
              <el-option v-for="item in splitterOptions" :key="item.name" :label="item.name" :value="item.name" />
            </el-select>
          </el-form-item>
        </div>

        <el-form-item :label="t('文件选择', 'File')">
          <el-upload
            drag
            :auto-upload="false"
            :show-file-list="true"
            :limit="1"
            :on-change="handleUploadChange"
            :on-remove="() => { uploadForm.file = null }"
          >
            <el-icon class="el-icon--upload"><Upload /></el-icon>
            <div class="el-upload__text">{{ t('拖拽文件到这里，或', 'Drop a file here or') }} <em>{{ t('点击选择文件', 'choose a file') }}</em></div>
            <template #tip>
              <div class="upload-tip">{{ t('支持 `txt / md / pdf / docx`，提交后将自动解析、切分并写入 Milvus。', 'Supports TXT, Markdown, PDF, and DOCX. Files are parsed, chunked, and indexed in Milvus.') }}</div>
            </template>
          </el-upload>
        </el-form-item>
      </el-form>

      <div class="toolbar-actions">
        <el-button type="primary" :icon="Upload" :loading="uploading" @click="submitUploadDocument">
          {{ t('上传并解析入库', 'Upload and index') }}
        </el-button>
      </div>
    </section>

    <section class="panel span-6 section-card">
      <div class="section-heading">
        <div>
          <div class="panel-kicker">Pipeline</div>
          <h3>{{ t('执行说明', 'Import steps') }}</h3>
        </div>
      </div>

      <div class="stack-list">
        <article class="stack-item">
          <div class="stack-title">{{ t('1. 上传源文件', '1. Upload a source file') }}</div>
          <p class="stack-text">{{ t('选择文档后，后端先保存原始文件，再根据文件类型选择解析器。', 'The backend saves the original file and selects a parser for its format.') }}</p>
        </article>
        <article class="stack-item">
          <div class="stack-title">{{ t('2. 自动切分', '2. Split into chunks') }}</div>
          <p class="stack-text">{{ t('系统根据结构化、半结构化或非结构化策略切分内容，并生成 Chunk 元数据。', 'The service selects a chunking strategy and creates metadata for each chunk.') }}</p>
        </article>
        <article class="stack-item">
          <div class="stack-title">{{ t('3. 写入向量库', '3. Write the vector index') }}</div>
          <p class="stack-text">{{ t('Chunk 内容完成向量化后写入 Milvus，同时把文档元数据记录到 PostgreSQL。', 'Embeddings are stored in Milvus and document metadata is stored in PostgreSQL.') }}</p>
        </article>
        <article class="stack-item">
          <div class="stack-title">{{ t('4. 跳转验证', '4. Inspect the results') }}</div>
          <p class="stack-text">{{ t('上传成功后可直接进入 Chunk 管理页，继续查看切分结果、检索命中和 Agent 溯源。', 'Open Chunks to review the indexed excerpts, retrieval results, and source references.') }}</p>
        </article>
      </div>
    </section>

    <section class="panel span-12 section-card">
      <div class="section-heading">
        <div>
          <div class="panel-kicker">Text Fallback</div>
          <h3>{{ t('纯文本快速入库', 'Import plain text') }}</h3>
        </div>
        <el-tag effect="dark">{{ t('备用方式', 'Alternative') }}</el-tag>
      </div>

      <el-form label-position="top">
        <el-form-item :label="t('文件名', 'File name')">
          <el-input v-model="textForm.filename" :placeholder="t('例如：product_rules.md', 'Example: product_rules.md')" />
        </el-form-item>
        <div class="form-grid two-columns">
          <el-form-item :label="t('知识库名称', 'Knowledge base')">
            <el-input v-model="textForm.knowledge_base" />
          </el-form-item>
          <el-form-item :label="t('切分策略', 'Chunking strategy')">
            <el-select v-model="textForm.preferred_splitter" clearable :placeholder="t('自动判断', 'Auto')">
              <el-option v-for="item in splitterOptions" :key="item.name" :label="item.name" :value="item.name" />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item :label="t('正文内容', 'Content')">
          <el-input v-model="textForm.content" type="textarea" :rows="10" :placeholder="t('直接粘贴需要入库的文本内容', 'Paste the text to import')" />
        </el-form-item>
      </el-form>

      <div class="toolbar-actions">
        <el-button type="primary" plain :icon="Check" :loading="textSubmitting" @click="submitTextDocument">
          {{ t('文本直接入库', 'Import text') }}
        </el-button>
      </div>
    </section>

    <section class="panel span-12 section-card">
      <div class="section-heading">
        <div>
          <div class="panel-kicker">Recent Documents</div>
          <h3>{{ t('最近入库结果', 'Recent imports') }}</h3>
        </div>
        <el-tag effect="dark">{{ documentStore.documents.length }} {{ t('份文档', 'documents') }}</el-tag>
      </div>

      <el-table v-loading="documentStore.loading" :data="latestDocuments" stripe>
        <el-table-column prop="filename" :label="t('文件名', 'File name')" min-width="240" />
        <el-table-column prop="knowledge_base" :label="t('知识库', 'Knowledge base')" min-width="120" />
        <el-table-column prop="file_type" :label="t('类型', 'Type')" width="100" />
        <el-table-column prop="chunk_count" label="Chunks" width="100" />
        <el-table-column :label="t('大小', 'Size')" width="120">
          <template #default="scope">{{ formatFileSize(scope.row.file_size) }}</template>
        </el-table-column>
        <el-table-column :label="t('更新时间', 'Updated at')" min-width="160">
          <template #default="scope">{{ formatDateTime(scope.row.updated_at) }}</template>
        </el-table-column>
        <el-table-column :label="t('操作', 'Actions')" width="260" fixed="right">
          <template #default="scope">
            <div class="inline-actions">
              <el-button size="small" @click="router.push({ name: 'chunks', query: { documentId: scope.row.id } })">
                {{ t('查看切分', 'View chunks') }}
              </el-button>
              <el-button size="small" @click="router.push({ name: 'retrieval', query: { keyword: scope.row.filename } })">
                {{ t('检索调试', 'Retrieval Console') }}
              </el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </section>
  </div>
</template>
