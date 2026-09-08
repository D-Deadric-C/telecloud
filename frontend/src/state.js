// Shared mutable app state. One module so views don't pass everything around.
// (currentUser lives on the session object in session.js.)

export const state = {
  // Directory navigation
  currentFolderId: null,
  folderContents: { folders: [], files: [] },
  navigationHistory: [], // [{ id, name }]
  allWorkspaceFolders: [], // flat [{ id, name, path, depth }] for move dropdown + breadcrumbs

  // Storage estimate (bytes)
  estimatedStorageUsed: 0,

  // Modal context: { type: 'file'|'folder', id, name }
  activeModalItem: null,

  // Browser UI
  viewMode: localStorage.getItem('view_mode') === 'grid' ? 'grid' : 'list',
  searchQuery: '',

  // Auth UI
  currentAuthTab: 'login',
  pendingVerificationEmail: localStorage.getItem('pending_verification_email') || null,

  // Public share
  publicShareToken: null,
  publicFileDetails: null,
};
