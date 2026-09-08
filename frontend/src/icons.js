// Curated icon set — only the icons actually referenced via data-lucide. Importing
// these by name (instead of the whole `lucide` icons object) lets the bundler
// tree-shake the other ~1700 icons out, cutting the bundle by an order of magnitude.
//
// Keys MUST be PascalCase: lucide's createIcons converts a data-lucide name
// (e.g. "folder-plus") to PascalCase ("FolderPlus") and looks it up here.

import {
  Mail, Lock, Eye, EyeOff, MailWarning, Send, Cloud, Files, Folder, Database,
  LogOut, FolderPlus, Upload, FolderOpen, AlertCircle, File, Download, X, Edit2,
  FolderInput, Trash2, Link2, Copy, Slash, CheckCircle, AlertTriangle, FileText,
  Video, FileType, Table, Image, Sun, Moon, Menu, EllipsisVertical,
  Search, LayoutGrid, List, UploadCloud, HardDrive, ShieldCheck, Zap,
  Music, Archive, FileCode, Sparkles, Star, Github, Linkedin,
} from 'lucide';

export const appIcons = {
  Mail, Lock, Eye, EyeOff, MailWarning, Send, Cloud, Files, Folder, Database,
  LogOut, FolderPlus, Upload, FolderOpen, AlertCircle, File, Download, X, Edit2,
  FolderInput, Trash2, Link2, Copy, Slash, CheckCircle, AlertTriangle, FileText,
  Video, FileType, Table, Image, Sun, Moon, Menu, EllipsisVertical,
  Search, LayoutGrid, List, UploadCloud, HardDrive, ShieldCheck, Zap,
  Music, Archive, FileCode, Sparkles, Star, Github, Linkedin,
};
