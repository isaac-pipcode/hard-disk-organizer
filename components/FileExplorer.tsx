import React, { useState, useEffect, useMemo } from 'react';
import { Search, File, FileImage, FileVideo, FileAudio, FileCode, FileText, HardDrive, Sparkles, Filter, Trash2 } from 'lucide-react';
import { FileItem, Drive } from '../types';
import { analyzeSearchQuery } from '../services/geminiService';
import { useLiveQuery } from 'dexie-react-hooks';
import { db, deleteFile } from '../services/db';

interface FileExplorerProps {
  drives: Drive[];
}

export const FileExplorer: React.FC<FileExplorerProps> = ({ drives }) => {
  // State
  const [searchQuery, setSearchQuery] = useState('');
  const [aiLoading, setAiLoading] = useState(false);
  const [activeTypeFilter, setActiveTypeFilter] = useState<string>('all');
  const [selectedDriveId, setSelectedDriveId] = useState<number | 'all'>('all');
  const [page, setPage] = useState(1);
  const PAGE_SIZE = 50;

  // Real-time db access
  const files = useLiveQuery(async () => {
    let collection = db.files.orderBy('id').reverse();

    if (selectedDriveId !== 'all') {
      collection = db.files.where('driveId').equals(selectedDriveId);
    }

    let result = await collection.toArray(); // Get all for client side filtering to be responsive

    // Apply Text Search
    if (searchQuery) {
      const lowerQ = searchQuery.toLowerCase();
      result = result.filter(f => f.name.toLowerCase().includes(lowerQ) || f.path.toLowerCase().includes(lowerQ));
    }

    // Apply Type Filter
    if (activeTypeFilter !== 'all') {
      result = result.filter(f => f.type === activeTypeFilter);
    }

    // Apply Drive Filter (redundant if we used where clause, but safe)
    if (selectedDriveId !== 'all') {
        result = result.filter(f => f.driveId === selectedDriveId);
    }

    return result;
  }, [searchQuery, activeTypeFilter, selectedDriveId]);

  // Pagination logic
  const paginatedFiles = useMemo(() => {
    if (!files) return [];
    const start = (page - 1) * PAGE_SIZE;
    return files.slice(start, start + PAGE_SIZE);
  }, [files, page]);

  const totalPages = files ? Math.ceil(files.length / PAGE_SIZE) : 0;

  useEffect(() => {
    setPage(1);
  }, [searchQuery, activeTypeFilter, selectedDriveId]);

  const handleGeminiSearch = async () => {
    if (!searchQuery) return;
    setAiLoading(true);
    const params = await analyzeSearchQuery(searchQuery);
    setAiLoading(false);
    
    if (params) {
      if (params.query) setSearchQuery(params.query);
      if (params.type) setActiveTypeFilter(params.type);
      if (params.minSizeMB || params.minSizeGB) {
        alert(`Gemini sugeriu filtrar por tamanho (${params.minSizeGB ? params.minSizeGB + 'GB' : params.minSizeMB + 'MB'}). Filtros de tamanho avançados podem ser adicionados em versões futuras.`);
      }
    }
  };

  const handleDelete = async (id: number, name: string) => {
    const confirmed = window.confirm(
        `Tem certeza que deseja remover "${name}" do catálogo?\n\nEsta ação não apaga o arquivo do seu disco, apenas remove o registro deste catálogo.`
    );
    
    if (confirmed) {
        try {
            await deleteFile(id);
        } catch (error) {
            console.error("Erro ao excluir arquivo:", error);
            alert("Não foi possível excluir o arquivo do catálogo.");
        }
    }
  };

  const formatSize = (bytes: number) => {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  };

  const getIcon = (type: string) => {
    switch (type) {
      case 'imagem': return <FileImage className="w-5 h-5 text-purple-400" />;
      case 'video': return <FileVideo className="w-5 h-5 text-red-400" />;
      case 'audio': return <FileAudio className="w-5 h-5 text-yellow-400" />;
      case 'codigo': return <FileCode className="w-5 h-5 text-green-400" />;
      case 'documento': return <FileText className="w-5 h-5 text-blue-400" />;
      default: return <File className="w-5 h-5 text-slate-400" />;
    }
  };

  return (
    <div className="bg-slate-800 rounded-xl border border-slate-700 shadow-lg flex flex-col h-[800px]">
      {/* Header / Toolbar */}
      <div className="p-4 border-b border-slate-700 flex flex-col gap-4">
        <div className="flex flex-col md:flex-row gap-3">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 w-4 h-4" />
            <input
              type="text"
              placeholder="Buscar arquivos..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full bg-slate-900 border border-slate-600 rounded-lg pl-10 pr-12 py-2 text-white focus:ring-2 focus:ring-blue-500 outline-none text-sm"
            />
            <button 
              onClick={handleGeminiSearch}
              disabled={!searchQuery || aiLoading}
              className="absolute right-2 top-1/2 -translate-y-1/2 p-1 hover:bg-slate-700 rounded-md transition-colors text-blue-400"
              title="Otimizar busca com IA"
            >
              {aiLoading ? <span className="animate-spin">✨</span> : <Sparkles className="w-4 h-4" />}
            </button>
          </div>
          
          <div className="flex gap-2 overflow-x-auto pb-2 md:pb-0">
             <select 
              value={selectedDriveId} 
              onChange={(e) => setSelectedDriveId(e.target.value === 'all' ? 'all' : Number(e.target.value))}
              className="bg-slate-900 border border-slate-600 text-white text-sm rounded-lg px-3 py-2 outline-none"
            >
              <option value="all">Todos os Discos</option>
              {drives.map(d => (
                <option key={d.id} value={d.id}>{d.name}</option>
              ))}
            </select>
            
            <select 
              value={activeTypeFilter} 
              onChange={(e) => setActiveTypeFilter(e.target.value)}
              className="bg-slate-900 border border-slate-600 text-white text-sm rounded-lg px-3 py-2 outline-none"
            >
              <option value="all">Todos os Tipos</option>
              <option value="imagem">Imagens</option>
              <option value="video">Vídeos</option>
              <option value="audio">Áudios</option>
              <option value="documento">Documentos</option>
              <option value="codigo">Código</option>
              <option value="executavel">Apps</option>
              <option value="arquivo">Compactos</option>
            </select>
          </div>
        </div>
        
        <div className="flex justify-between items-center text-xs text-slate-400">
            <span>{files ? `${files.length} arquivos encontrados` : 'Carregando...'}</span>
            <div className="flex gap-2">
                <button 
                    disabled={page === 1} 
                    onClick={() => setPage(p => p - 1)}
                    className="px-2 py-1 bg-slate-700 rounded hover:bg-slate-600 disabled:opacity-50"
                >
                    Anterior
                </button>
                <span className="py-1">Pág {page} de {totalPages || 1}</span>
                <button 
                    disabled={page >= totalPages} 
                    onClick={() => setPage(p => p + 1)}
                    className="px-2 py-1 bg-slate-700 rounded hover:bg-slate-600 disabled:opacity-50"
                >
                    Próximo
                </button>
            </div>
        </div>
      </div>

      {/* File List */}
      <div className="flex-1 overflow-y-auto p-2">
        <table className="w-full text-left border-collapse">
            <thead className="text-xs text-slate-400 uppercase bg-slate-900/50 sticky top-0 backdrop-blur-sm z-10">
                <tr>
                    <th className="px-4 py-3 rounded-tl-lg">Nome</th>
                    <th className="px-4 py-3">Disco</th>
                    <th className="px-4 py-3 text-right">Tamanho</th>
                    <th className="px-4 py-3 text-center">Tipo</th>
                    <th className="px-4 py-3 rounded-tr-lg w-[50px]"></th>
                </tr>
            </thead>
            <tbody className="text-sm divide-y divide-slate-700/50">
                {paginatedFiles?.map(file => (
                    <tr key={file.id} className="hover:bg-slate-700/30 transition-colors group">
                        <td className="px-4 py-2">
                            <div className="flex items-center gap-3">
                                {getIcon(file.type)}
                                <div className="flex flex-col overflow-hidden">
                                    <span className="font-medium text-slate-200 truncate max-w-[200px] sm:max-w-sm md:max-w-md" title={file.name}>{file.name}</span>
                                    <span className="text-xs text-slate-500 truncate max-w-[200px] sm:max-w-sm md:max-w-md" title={file.path}>{file.path}</span>
                                </div>
                            </div>
                        </td>
                        <td className="px-4 py-2 text-slate-400 whitespace-nowrap">
                           <div className="flex items-center gap-1">
                             <HardDrive className="w-3 h-3" />
                             {file.driveName}
                           </div>
                        </td>
                        <td className="px-4 py-2 text-right text-slate-400 whitespace-nowrap font-mono text-xs">
                            {formatSize(file.size)}
                        </td>
                        <td className="px-4 py-2 text-center">
                            <span className={`text-[10px] uppercase px-2 py-1 rounded-full font-semibold
                                ${file.type === 'imagem' ? 'bg-purple-500/10 text-purple-400' : 
                                  file.type === 'video' ? 'bg-red-500/10 text-red-400' : 
                                  file.type === 'codigo' ? 'bg-green-500/10 text-green-400' : 
                                  'bg-slate-500/10 text-slate-400'}`}>
                                {file.extension}
                            </span>
                        </td>
                        <td className="px-4 py-2 text-center">
                            <button
                                onClick={() => file.id && handleDelete(file.id, file.name)}
                                className="p-1.5 text-slate-500 hover:text-red-400 hover:bg-red-500/10 rounded-md transition-colors opacity-0 group-hover:opacity-100"
                                title="Remover do catálogo"
                            >
                                <Trash2 className="w-4 h-4" />
                            </button>
                        </td>
                    </tr>
                ))}
                {(!paginatedFiles || paginatedFiles.length === 0) && (
                    <tr>
                        <td colSpan={5} className="px-4 py-8 text-center text-slate-500">
                            Nenhum arquivo encontrado com os filtros atuais.
                        </td>
                    </tr>
                )}
            </tbody>
        </table>
      </div>
    </div>
  );
};