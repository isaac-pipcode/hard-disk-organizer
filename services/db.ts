import Dexie, { Table } from 'dexie';
import { Drive, FileItem } from '../types';

export class FileCatalogDB extends Dexie {
  drives!: Table<Drive, number>;
  files!: Table<FileItem, number>;

  constructor() {
    super('FileCatalogDB');
    // Explicitly cast to any to resolve TS error: Property 'version' does not exist on type 'FileCatalogDB'
    (this as any).version(1).stores({
      drives: '++id, name',
      files: '++id, driveId, name, path, size, extension, type, [driveId+type]'
    });
  }
}

export const db = new FileCatalogDB();

export const addDriveAndFiles = async (driveName: string, files: FileList): Promise<void> => {
  const driveDate = new Date();
  let totalSize = 0;
  
  // Calculate totals first (lightweight)
  for (let i = 0; i < files.length; i++) {
    totalSize += files[i].size;
  }

  // Explicitly cast to any to resolve TS error: Property 'transaction' does not exist on type 'FileCatalogDB'
  await (db as any).transaction('rw', db.drives, db.files, async () => {
    const driveId = await db.drives.add({
      name: driveName,
      dateScanned: driveDate,
      totalFiles: files.length,
      totalSize: totalSize
    });

    // Batch insert files
    const BATCH_SIZE = 2000;
    const fileItems: FileItem[] = [];

    for (let i = 0; i < files.length; i++) {
      const file = files[i];
      const path = file.webkitRelativePath || file.name;
      const extension = file.name.split('.').pop()?.toLowerCase() || 'none';
      
      let type = 'outros';
      if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp'].includes(extension)) type = 'imagem';
      else if (['mp4', 'mkv', 'avi', 'mov', 'webm'].includes(extension)) type = 'video';
      else if (['mp3', 'wav', 'flac', 'aac', 'ogg'].includes(extension)) type = 'audio';
      else if (['pdf', 'doc', 'docx', 'txt', 'md', 'xls', 'xlsx', 'ppt'].includes(extension)) type = 'documento';
      else if (['zip', 'rar', '7z', 'tar', 'gz'].includes(extension)) type = 'arquivo';
      else if (['exe', 'msi', 'bat', 'sh', 'bin'].includes(extension)) type = 'executavel';
      else if (['js', 'ts', 'html', 'css', 'json', 'py', 'java'].includes(extension)) type = 'codigo';

      fileItems.push({
        driveId: Number(driveId),
        driveName: driveName,
        name: file.name,
        path: path,
        size: file.size,
        extension: extension,
        type: type
      });

      if (fileItems.length >= BATCH_SIZE) {
        await db.files.bulkAdd(fileItems);
        fileItems.length = 0;
      }
    }
    
    if (fileItems.length > 0) {
      await db.files.bulkAdd(fileItems);
    }
  });
};

export const deleteFile = async (fileId: number): Promise<void> => {
  await (db as any).transaction('rw', db.files, db.drives, async () => {
    const file = await db.files.get(fileId);
    if (file) {
      await db.files.delete(fileId);
      const drive = await db.drives.get(file.driveId);
      if (drive) {
        await db.drives.update(file.driveId, {
          totalFiles: Math.max(0, drive.totalFiles - 1),
          totalSize: Math.max(0, drive.totalSize - file.size)
        });
      }
    }
  });
};