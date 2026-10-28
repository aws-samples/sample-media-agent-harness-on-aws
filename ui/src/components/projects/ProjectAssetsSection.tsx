/**
 * The Selected_Project's `Assets` section on the Projects_Surface (media-harness-projects-access Component 11,
 * Req 10.6; media-harness-project-ingestion Components 4 and 5).
 *
 *   * Toolbar: ONE row, identical in the Icons and List views (toggling never moves a control): Search (the only
 *     part that grows or shrinks), Folder (when the Project has folders), Type (`All types`, the kinds, and
 *     `Analysis files`), the view toggle, and `Upload ▾` (`Files`: a multi-file picker; `Folder`: a directory
 *     picker). It used to render the Folder select in the Icons view only, inside a wrapping row next to a heading
 *     and a fixed-width search, so switching views added/removed a control and re-wrapped the row. The "Add S3
 *     location" form was removed from the UI for now (the `/s3-locations` route stays). The whole section is
 *     also a drop zone for files AND folders (walked recursively,
 *     each file keeping its relative folder). Uploads go through the tab's upload queue, so they keep going when
 *     the surface closes, and their per-file list (a summary row past 50 files) shows here while this Project is
 *     selected. A batch of more than 200 files into a Project with indexing ON asks first: each is analyzed.
 *   * The Assets grid: newest first, `Load more`, a card per asset (thumbnail or kind icon, name, kind, size, a
 *     status badge only while it needs attention), Delete behind each card's overflow menu with a confirmation, and a
 *     10 s poll while any asset is still on its way to a final status. Folders: the Folder select filters in both
 *     views; with `All folders` the List view groups rows under collapsible folder headers (top level first, then by
 *     path).
 *   * Session structure (asset-lineage R11): a session's outputs sit in `Sessions/<title> · <date>` folders (collapsed
 *     by default in the List view). `analysis` / `intermediate` files are hidden unless the Type is `Analysis files`
 *     (which shows just them) or their `Sessions/…` folder is opened (`assetSelection.showsInGrid`).
 *   * Bulk delete (R10): a checkbox per card and row, shown on hover/focus and on every item once one is selected.
 *     While anything is selected the toolbar row is replaced, in place and at the same height, by
 *     `N selected · Select all · Delete · Clear` (`Select all` = what is on screen: the current search, type, folder
 *     and expanded folders); one `Delete N files` confirmation, then the per-asset remove for each, 6 at a time, with
 *     `Deleting k of N`, a per-file failure list, and one refresh.
 *   * `No assets yet` when the Project has none.
 *
 *   * Folders (organize): the full surface has a folder tree (`FolderTree`) in place of the Folder select — All
 *     files, the user's folders, then `Chats/…` last. Picking a folder loads ITS files from the server (not just the
 *     loaded pages). Files move by `Move to…` on a selection (`FolderPicker`, find-or-create) or by dragging cards
 *     onto a folder; New folder and Rename work in the tree. A move is metadata only and offers Undo. Browse-only
 *     mode (the chat's `project-browser` tab) keeps the Folder select and changes nothing.
 *
 * A registered `assets` slot (`tabSlots`) still replaces the grid.
 */
import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { theme } from '../../styles/tokens';
import Button from '../ui/Button';
import ConfirmDialog from '../ConfirmDialog';
import FolderIcon from '../icons/FolderIcon';
import UploadIcon from '../icons/UploadIcon';
import AssetCard, { AssetListHeader, type AssetLayout } from './AssetCard';
import { nextSort, readStoredSort, sortAssets, storeSort, type AssetSort, type AssetSortKey } from './assetSort';
import GridViewIcon from '../icons/GridViewIcon';
import ListViewIcon from '../icons/ListViewIcon';
import UploadFileList from './UploadFileList';
import { useProjectAssets } from './useProjectAssets';
import { getProjectTab, type ProjectTabProps } from './tabSlots';
import ChevronDownIcon from '../icons/ChevronDownIcon';
import {
  MOVE_ASSETS_MAX, createFolder as createFolderApi, getProject, listFolders, moveAssets, removeAsset, renameFolder, retryAsset, retryRebuild, searchAssets,
  type AssetView, type FolderCount, type ProjectAssetKind,
} from '../../projects/api';
import { buildFolderTree, flattenFolders, folderErrorText, type FolderNode, folderName, isWithin, parentFolder } from '../../projects/folderPaths';
import { onPendingFoldersChange, readPendingFolders, writePendingFolders } from '../../projects/pendingFolders';
import { FolderPicker } from './FolderPicker';
import { FolderTree } from './FolderTree';
import { SavedMoments } from './SavedMoments';
import { draggedAssetIds, isAssetDrag } from './assetDrag';
import { ImportPanel } from './ImportPanel';
import { dragHasFiles, filesFromDirectoryInput, readDroppedFiles } from '../../projects/upload/folders';
import { UPLOAD_SUMMARY_THRESHOLD } from './UploadFileList';
import { assetFolder } from './AssetCard';
import { findDuplicates, type Duplicate } from '../../projects/upload/duplicates';
import DuplicatesDialog from './DuplicatesDialog';
import { clearFinished, dismiss, isTerminal, startBatch, useProjectUploads } from '../../projects/upload/uploadQueue';
import { useRovingMenu } from '../ui/useRovingMenu';
import {
  BULK_DELETE_MESSAGE,
  bulkDeleteNames,
  bulkDeleteTitle,
  deleteInParallel,
  isAnalysisFile,
  isMetadataFile,
  isSessionFolder,
  showsInGrid,
  type BulkDeleteFailure,
} from './assetSelection';

/** localStorage key for the Assets view (`grid` | `list`), shared by every Project like Finder's. */
export const ASSETS_VIEW_KEY = 'projects-assets-view';

/** The persisted Assets view; `grid` when unset or unreadable. */
export function readAssetsView(): AssetLayout {
  try {
    return localStorage.getItem(ASSETS_VIEW_KEY) === 'list' ? 'list' : 'grid';
  } catch {
    return 'grid';
  }
}

export const ASSETS_EMPTY_TITLE = 'No assets yet';
export const ASSETS_EMPTY_BODY =
  'Upload files to get started, or drop files or folders here. Files you attach in chat are added here too, unless you choose "this chat only".';

/** The one upload control: a menu of {@link UPLOAD_FILES_ITEM} and {@link UPLOAD_FOLDER_ITEM}. */
/** The empty state of a browse-only grid, which offers no upload. */
export const ASSETS_EMPTY_BODY_BROWSE = 'This project has no files yet. Add some from the Projects page or attach them in chat.';
export const UPLOAD_BUTTON_LABEL = 'Upload';
/** Its accessible name: starts with the visible label, and differs from a registered `upload` header slot's `Upload`. */
export const UPLOAD_BUTTON_NAME = 'Upload files or a folder';
export const UPLOAD_FILES_ITEM = 'Files';
export const UPLOAD_FOLDER_ITEM = 'Folder';
export const UPLOAD_IMPORT_ITEM = 'Import from S3…';
export const DROP_HINT = 'Drop files or folders here';

/** The Type select's extra value: only `analysis` / `intermediate` files (hidden under `All types`). */
export const ANALYSIS_TYPE = 'analysis';
export const ANALYSIS_TYPE_LABEL = 'Analysis files';
/**
 * The Type select's other extra value: only `kind: 'data'` files — the machine metadata/analysis data a
 * session or workflow writes (shots.json, transcripts, probe.json, compliance JSON, …). Distinct from
 * `Analysis files` (a ROLE filter, which also catches intermediate image frames): `Metadata` is the KIND
 * cut, and it surfaces the `role: analysis` data files that `All types` hides. A client-side filter, like
 * `ANALYSIS_TYPE` — not a server kind (its `serverKind` is `''`).
 */
export const DATA_TYPE = 'data';
export const DATA_TYPE_LABEL = 'Metadata';
export type AssetTypeFilter = '' | ProjectAssetKind | typeof ANALYSIS_TYPE | typeof DATA_TYPE;
export const ANALYSIS_HIDDEN_HINT = `Only analysis files here. Choose ${ANALYSIS_TYPE_LABEL} under All types to see them.`;

/** The bulk-delete progress line. */
export function bulkDeleteProgress(done: number, total: number): string {
  return `Deleting ${Math.min(done + 1, total).toLocaleString('en-US')} of ${total.toLocaleString('en-US')}`;
}

/** A batch above this many files into a Project with indexing ON asks before uploading. */
export const COST_CONFIRM_THRESHOLD = 200;

/** The cost confirmation's message. */
export function costConfirmMessage(n: number): string {
  return `${n.toLocaleString('en-US')} files will be analyzed (media workflows run for each). Continue?`;
}

/** Folder filter values that are not a path. */
export const ALL_FOLDERS = '';
export const TOP_LEVEL = '\u0000top';

export interface FolderGroup {
  /** `''` for the top level. */
  folder: string;
  assets: AssetView[];
}

const byPath = (a: string, b: string) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });

/**
 * Assets grouped by folder: the top level first, then each folder path in order (a nested path is one group,
 * `Footage/Day 1`, not a tree). Inside a group the incoming (newest-first) order is kept.
 */
export function groupByFolder(assets: readonly AssetView[]): FolderGroup[] {
  const groups = new Map<string, AssetView[]>();
  for (const a of assets) {
    const f = assetFolder(a);
    const g = groups.get(f);
    if (g) g.push(a);
    else groups.set(f, [a]);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => (a === '' ? -1 : b === '' ? 1 : byPath(a, b)))
    .map(([folder, list]) => ({ folder, assets: list }));
}

/** The Delete confirmation's message for one asset. */
/** What Delete folder says will happen: nothing is deleted; the contents move up to the parent. */
export function deleteFolderMessage(folder: string, tree: readonly FolderNode[]): string {
  const find = (ns: readonly FolderNode[]): FolderNode | undefined => {
    for (const n of ns) {
      if (n.path === folder) return n;
      const hit = isWithin(folder, n.path) ? find(n.children) : undefined;
      if (hit) return hit;
    }
    return undefined;
  };
  const node = find(tree);
  const total = node?.total ?? 0;
  const subs = node?.children.length ?? 0;
  const parent = parentFolder(folder);
  const where = parent ? `“${folderName(parent)}”` : 'Top level';
  const what = [total ? `its ${total.toLocaleString('en-US')} item${total === 1 ? '' : 's'}` : '',
    subs ? `${subs} subfolder${subs === 1 ? '' : 's'}` : ''].filter(Boolean).join(' and ');
  const verb = total + subs === 1 ? 'moves' : 'move';
  return `${what ? `${what[0].toUpperCase()}${what.slice(1)} ${verb}` : 'Nothing moves'} to ${where}. No file is deleted, and you can undo this.`;
}

export function deleteAssetMessage(name: string): string {
  return `Delete ${name}? This removes it from the project and from chats it was attached to.`;
}

/** The grid's starting filter (a `project-browser` tab seeds it from the agent's `browse_project` call). */
export interface AssetsInitialFilter {
  q?: string;
  folder?: string;
  kind?: string;
}

export interface ProjectAssetsSectionProps extends ProjectTabProps {
  /**
   * Browse only (the chat's `project-browser` tab): search, folder/type filters, the view toggle and Load more —
   * and nothing that changes the Project. No upload (button, menu, drop), no delete (single or bulk), no retry,
   * no rebuild, and a registered `assets` slot does not replace the grid.
   */
  browseOnly?: boolean;
  initialFilter?: AssetsInitialFilter;
  /** An asset's ⋯ → Lineage (the Projects Lineage view focused on it). Absent where lineage is off. */
  onShowLineage?(assetId: string): void;
}

const FILTER_KINDS: readonly string[] = ['video', 'image', 'audio', 'document', ANALYSIS_TYPE, DATA_TYPE];

export default function ProjectAssetsSection(props: ProjectAssetsSectionProps) {
  const { project, onNotFound, sessionId, indexing: knownIndexing, browseOnly = false, initialFilter, onShowLineage } = props;
  const Slot = browseOnly ? undefined : getProjectTab('assets');
  const fileInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [openError, setOpenError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<AssetView | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const uploads = useProjectUploads(project.id);
  const assets = useProjectAssets(project.id, onNotFound);

  // Filename search + type filter (works whether indexing is on or off: it searches names, not content).
  const [query, setQuery] = useState(() => initialFilter?.q ?? '');
  const [kind, setKind] = useState<AssetTypeFilter>(
    () => (FILTER_KINDS.includes(initialFilter?.kind ?? '') ? (initialFilter!.kind as AssetTypeFilter) : ''));
  // `Analysis files` is a client-side role filter, not a server kind. `Metadata` is a client-side kind
  // filter (`kind === 'data'`), also not a server kind. Both map `serverKind` to `''`.
  const showAnalysis = kind === ANALYSIS_TYPE;
  const showData = kind === DATA_TYPE;
  const serverKind: '' | ProjectAssetKind = showAnalysis || showData ? '' : kind;
  const [found, setFound] = useState<{ assets: AssetView[]; truncated: boolean; total?: number; cursor?: string } | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [searchSeq, setSearchSeq] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  // Folders. The tree (full surface) or the Folder select (browse-only) picks one; a picked folder is fetched from
  // the server, so it shows every file in it rather than those among the loaded pages. `TOP_LEVEL` is the root.
  const [folderFilter, setFolderFilter] = useState<string>(() => initialFilter?.folder || ALL_FOLDERS);
  const serverFolder: string | undefined = folderFilter === ALL_FOLDERS ? undefined
    : folderFilter === TOP_LEVEL ? '' : folderFilter;
  const searching = query.trim() !== '' || serverKind !== '' || serverFolder !== undefined;
  useEffect(() => {
    if (!searching) {
      setFound(null);
      setSearchError(null);
      return;
    }
    let live = true;
    const t = setTimeout(() => {
      // A new query always starts at page one (no cursor); its ranked order is deterministic, so paging
      // over it is stable.
      void searchAssets(project.id, { q: query, kind: serverKind || undefined, folder: serverFolder }).then((r) => {
        if (!live) return;
        if (r.ok) { setFound(r.value); setSearchError(null); }
        else if (r.kind === 'not_found') onNotFound();
        else setSearchError(r.kind === 'error' ? r.message : 'Search failed; try again.');
      });
    }, 250);
    return () => { live = false; clearTimeout(t); };
  }, [project.id, query, serverKind, serverFolder, searching, searchSeq, onNotFound]);
  // Load the next ranked page and append it; the cursor pins the exact (q, kind) so a page always extends
  // the same result set.
  const loadMoreSearch = useCallback(() => {
    const cursor = found?.cursor;
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    void searchAssets(project.id, { q: query, kind: serverKind || undefined, folder: serverFolder }, cursor).then((r) => {
      setLoadingMore(false);
      if (r.ok) setFound((cur) => (cur ? { ...r.value, assets: [...cur.assets, ...r.value.assets] } : r.value));
      else if (r.kind === 'not_found') onNotFound();
      else setSearchError(r.kind === 'error' ? r.message : 'Search failed; try again.');
    });
  }, [found?.cursor, loadingMore, project.id, query, serverKind, serverFolder, onNotFound]);
  const [view, setView] = useState<AssetLayout>(readAssetsView);
  const chooseView = (v: AssetLayout) => {
    setView(v);
    try { localStorage.setItem(ASSETS_VIEW_KEY, v); } catch { /* storage full / disabled: in-memory only */ }
  };
  // List-view column sort (Name / Type / Size headers), remembered like the view. `null` = newest first.
  const [sort, setSort] = useState<AssetSort | null>(readStoredSort);
  const onSort = (key: AssetSortKey) => setSort((cur) => {
    const next = nextSort(cur, key);
    storeSort(next);
    return next;
  });

  // Folders. The Folder select filters in BOTH views (it is part of the one fixed toolbar); with `All folders` the
  // List view groups by every folder (collapsible headers).
  // List-view folders the user toggled away from their default: other folders start open, `Sessions/…` closed.
  const [toggled, setToggled] = useState<ReadonlySet<string>>(() => new Set());
  const folderOpen = (f: string) => (isSessionFolder(f) ? toggled.has(f) : !toggled.has(f));
  const listed = searching ? (found?.assets ?? []) : assets.assets;
  const folderOptions = useMemo(() => {
    const set = new Set<string>();
    for (const a of [...assets.assets, ...(found?.assets ?? [])]) {
      const f = assetFolder(a);
      if (f) set.add(f);
    }
    if (folderFilter !== ALL_FOLDERS && folderFilter !== TOP_LEVEL) set.add(folderFilter);
    return [...set].sort(byPath);
  }, [assets.assets, found, folderFilter]);
  // The complete folder list (`GET …/assets/folders`) plus folders created here that hold nothing yet. Refreshed on
  // open, after every move or rename, and when an upload batch finishes.
  // The tree needs room: the Projects pane shares the screen with the Renderer, so below TREE_MIN_WIDTH the grid
  // keeps the Folder select instead (the same server-backed pick, just a narrower control).
  const sectionRef = useRef<HTMLElement>(null);
  const sectionWidth = useElementWidth(sectionRef);
  // The folder list docks beside the grid when there is room AND the user has not collapsed it (remembered);
  // otherwise the same tree opens from the Folder button.
  const roomForTree = sectionWidth === null || sectionWidth >= TREE_MIN_WIDTH;
  const [treeCollapsed, setTreeCollapsedState] = useState<boolean>(() => {
    try { return localStorage.getItem(TREE_COLLAPSED_KEY) === '1'; } catch { return false; }
  });
  const setTreeCollapsed = (v: boolean) => {
    setTreeCollapsedState(v);
    try { localStorage.setItem(TREE_COLLAPSED_KEY, v ? '1' : '0'); } catch { /* storage off */ }
  };
  // Browse-only (the Renderer's Browse tab) docks the same tree READ-ONLY: navigate and collapse, nothing more.
  const showTree = roomForTree && !treeCollapsed;
  const [folderCounts, setFolderCounts] = useState<FolderCount[]>([]);
  const [rootCount, setRootCount] = useState<number | undefined>(undefined);
  // Empty folders made here (New folder) exist only on this device until something is moved in — remembered per
  // project, so a reload or a remount never loses one (the user made it to move files INTO).
  const [pendingFolders, setPendingFoldersState] = useState<string[]>(() => readPendingFolders(project.id));
  useEffect(() => {
    setPendingFoldersState(readPendingFolders(project.id));
    return onPendingFoldersChange((pid) => { if (pid === project.id) setPendingFoldersState(readPendingFolders(project.id)); });
  }, [project.id]);
  const setPendingFolders = useCallback((next: string[] | ((cur: string[]) => string[])) => {
    setPendingFoldersState((cur) => {
      const value = typeof next === 'function' ? next(cur) : next;
      if (JSON.stringify(value) !== JSON.stringify(cur)) queueMicrotask(() => writePendingFolders(project.id, value));
      return value;
    });
  }, [project.id]);
  const [foldersSeq, setFoldersSeq] = useState(0);
  const finishedUploads = uploads.filter((u) => isTerminal(u.status)).length;
  useEffect(() => {
    let live = true;
    void listFolders(project.id).then((r) => {
      if (live && r.ok) { setFolderCounts(r.value.folders); setRootCount(r.value.rootCount); }
    });
    return () => { live = false; };
  }, [project.id, browseOnly, foldersSeq, finishedUploads]);
  const folderTree = useMemo(() => buildFolderTree(folderCounts, pendingFolders), [folderCounts, pendingFolders]);
  // The open folder's subfolders, as tiles above its files (a folder whose files are all in subfolders is not
  // "empty"). Not at All files or Top level, where they would only repeat the tree.
  const [dropOn, setDropOn] = useState<string | null>(null);
  const subfolders = useMemo(() => {
    if (folderFilter === ALL_FOLDERS || folderFilter === TOP_LEVEL) return [];
    const find = (ns: readonly FolderNode[]): FolderNode | undefined => {
      for (const n of ns) {
        if (n.path === folderFilter) return n;
        if (isWithin(folderFilter, n.path)) return find(n.children);
      }
      return undefined;
    };
    return find(folderTree)?.children ?? [];
  }, [folderFilter, folderTree]);
  // The user's own folders first, chat folders after: there are many chat folders, and moving INTO one is rare.
  const allFolderPaths = useMemo(() => {
    const all = flattenFolders(folderTree);
    return [...all.filter((f) => !isSessionFolder(f)), ...all.filter((f) => isSessionFolder(f))];
  }, [folderTree]);
  // An empty folder exists only here until something is moved into it; once the server lists it, drop it.
  useEffect(() => {
    setPendingFolders((cur) => cur.filter((p) => !folderCounts.some((f) => isWithin(f.folder, p))));
  }, [folderCounts, setPendingFolders]);
  const filtering = folderFilter !== ALL_FOLDERS;
  const inFolder = filtering
    ? listed.filter((a) => (folderFilter === TOP_LEVEL ? assetFolder(a) === '' : assetFolder(a) === folderFilter))
    : listed;
  // Any filter (search, type, folder) makes the List view a flat list; only the unfiltered List view groups.
  const grouped = view === 'list' && !searching && !filtering && !showAnalysis && !showData;
  // The opened folder shows everything in it: the filtered folder, or (List view, All folders) each row's own folder,
  // whose header decides whether it is open. `Analysis files` shows only those; `Metadata` only `kind: data`.
  const openFolder = filtering && folderFilter !== TOP_LEVEL ? folderFilter : null;
  const shown = inFolder.filter((a) => (showData
    ? isMetadataFile(a)
    : showAnalysis
      ? isAnalysisFile(a)
      : showsInGrid(a, false, grouped ? assetFolder(a) : openFolder)));
  const hiddenCount = inFolder.length - shown.length;
  // The sort orders the List view only (the Icons view has no headers to show or clear it). Grouping keeps the
  // sorted order inside every folder.
  const ordered = useMemo(() => (view === 'list' ? sortAssets(shown, sort) : shown), [view, shown, sort]);
  const groups = useMemo(() => (grouped ? groupByFolder(ordered) : null), [grouped, ordered]);
  const toggleFolder = (f: string) => setToggled((cur) => {
    const next = new Set(cur);
    if (next.has(f)) next.delete(f);
    else next.add(f);
    return next;
  });

  // Bulk selection. Only what is on screen counts (a collapsed folder's rows drop out until it is opened again), so
  // `Delete N files` never removes a file the user cannot see. A new search, type, folder, or view starts a new
  // selection.
  const [selected, setSelected] = useState<ReadonlySet<string>>(() => new Set());
  const [bulkConfirm, setBulkConfirm] = useState<AssetView[] | null>(null);
  const [bulkProgress, setBulkProgress] = useState<{ done: number; total: number } | null>(null);
  const [bulkFailures, setBulkFailures] = useState<BulkDeleteFailure[]>([]);
  const onScreen = groups
    ? groups.flatMap((g) => (g.folder === '' || folderOpen(g.folder) ? g.assets : []))
    : shown;
  const selectedOnScreen = onScreen.filter((a) => selected.has(a.assetId));
  const allSelected = onScreen.length > 0 && selectedOnScreen.length === onScreen.length;
  // Selection mode: the toolbar row becomes the selection bar, and every item shows its checkbox.
  const selecting = selectedOnScreen.length > 0 || !!bulkProgress;
  useEffect(() => {
    setSelected(new Set());
  }, [project.id, query, kind, folderFilter, view]);
  const moveFromCard = useRef(false);
  // Shift-click selects (or clears) everything between the last card picked and this one, in screen order.
  const pickAnchor = useRef<string | null>(null);
  const onScreenRef = useRef<AssetView[]>([]);
  const setOneSelected = useCallback((a: AssetView, on: boolean, range?: boolean) => {
    const list = onScreenRef.current;
    const from = range && pickAnchor.current ? list.findIndex((x) => x.assetId === pickAnchor.current) : -1;
    const to = list.findIndex((x) => x.assetId === a.assetId);
    const ids = from >= 0 && to >= 0
      ? list.slice(Math.min(from, to), Math.max(from, to) + 1).map((x) => x.assetId) : [a.assetId];
    pickAnchor.current = a.assetId;
    setSelected((cur) => {
      const next = new Set(cur);
      for (const id of ids) { if (on) next.add(id); else next.delete(id); }
      return next;
    });
  }, []);
  onScreenRef.current = onScreen;
  const selectAll = () => setSelected(new Set(onScreen.map((a) => a.assetId)));

  // ---- Organize: move, undo, rename, create. Every move is metadata only (nothing is copied or re-indexed).
  const [moveOpen, setMoveOpen] = useState(false);
  const [organizeError, setOrganizeError] = useState<string | null>(null);
  // The last move, for Undo: where each moved asset was before (`''` = the root).
  const [lastMove, setLastMove] = useState<{ label: string; to: string; from: Map<string, string[]> } | null>(null);
  useEffect(() => {
    if (!lastMove) return;
    const t = setTimeout(() => setLastMove(null), 10000);
    return () => clearTimeout(t);
  }, [lastMove]);
  const refreshAfterOrganize = useCallback(() => {
    setFoldersSeq((n) => n + 1);
    setSearchSeq((n) => n + 1);
    void assets.reload();
  }, [assets]);
  const knownAsset = (id: string) => assets.assets.find((a) => a.assetId === id) ?? found?.assets.find((a) => a.assetId === id);
  /** Move `ids` to `folder` in batches; returns the ids that moved, and says so when some could not. */
  const runMove = useCallback(async (ids: string[], folder: string): Promise<string[]> => {
    const moved: string[] = [];
    let failed = 0;
    for (let i = 0; i < ids.length; i += MOVE_ASSETS_MAX) {
      const r = await moveAssets(project.id, ids.slice(i, i + MOVE_ASSETS_MAX), folder);
      if (r.ok) { moved.push(...r.value.moved); failed += r.value.failed.length; }
      else if (r.kind === 'not_found') { onNotFound(); return moved; }
      else { setOrganizeError(r.kind === 'error' ? r.message : 'Moving failed; try again.'); return moved; }
    }
    if (failed > 0) {
      setOrganizeError(`Couldn't move ${failed.toLocaleString('en-US')} file${failed === 1 ? '' : 's'}: removed, or no longer in this project.`);
    }
    return moved;
  }, [project.id, onNotFound]);
  const moveTo = useCallback(async (ids: string[], folder: string) => {
    setMoveOpen(false);
    moveFromCard.current = false;
    setOrganizeError(null);
    const before = new Map<string, string[]>();
    for (const id of ids) {
      const a = knownAsset(id);
      const f = a ? assetFolder(a) : '';
      if (f === folder) continue;
      before.set(f, [...(before.get(f) ?? []), id]);
    }
    const todo = [...before.values()].flat();
    if (todo.length === 0) return;
    const moved = await runMove(todo, folder);
    setSelected(new Set());
    if (moved.length) {
      const movedSet = new Set(moved);
      for (const [f, list] of before) before.set(f, list.filter((id) => movedSet.has(id)));
      const name = moved.length === 1 ? (knownAsset(moved[0])?.displayName ?? '1 file') : `${moved.length} files`;
      setLastMove({ label: `Moved ${name} to ${folder || 'Top level'}`, to: folder, from: before });
    }
    refreshAfterOrganize();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runMove, refreshAfterOrganize, assets.assets, found]);
  const undoMove = useCallback(async () => {
    const m = lastMove;
    setLastMove(null);
    if (!m) return;
    for (const [folder, ids] of m.from) if (ids.length) await runMove(ids, folder);
    refreshAfterOrganize();
  }, [lastMove, runMove, refreshAfterOrganize]);
  const createFolder = useCallback((folder: string) => {
    // Shown at once (and kept on this device); recorded on the server so every member and device sees it too.
    setPendingFolders((cur) => (cur.includes(folder) ? cur : [...cur, folder]));
    setFolderFilter(folder);
    void createFolderApi(project.id, folder).then((r) => {
      if (r.ok) setFoldersSeq((n) => n + 1);
      else if (r.kind === 'error') setOrganizeError(folderErrorText(r.message) ?? r.message);
    });
  }, [project.id, setPendingFolders]);
  /**
   * Re-file everything in `from` (and below) under `to` — a rename, or with `to` = its parent, a delete. Returns
   * where each item was, for Undo, or `null` when it failed (the error is shown, and nothing on screen moves).
   */
  const relocateFolder = useCallback(async (from: string, to: string): Promise<Map<string, string[]> | null> => {
    setOrganizeError(null);
    const before = new Map<string, string[]>();
    if (folderCounts.some((f) => isWithin(f.folder, from))) {
      for (let guard = 0; guard < 100; guard++) {
        const r = await renameFolder(project.id, from, to);
        if (!r.ok) {
          if (r.kind === 'not_found') onNotFound();
          else {
            const msg = r.kind === 'error' ? r.message : '';
            setOrganizeError(folderErrorText(msg) ?? (msg || 'That didn’t work; try again.'));
          }
          refreshAfterOrganize();
          return before.size ? before : null;
        }
        for (const m of r.value.moves ?? []) before.set(m.from, [...(before.get(m.from) ?? []), m.assetId]);
        if (r.value.remaining === 0) break;
      }
    }
    // Empty folders made on this device move (or go) with it.
    setPendingFolders((cur) => [...new Set(cur.map((p) => (isWithin(p, from) ? [to, p.slice(from.length + 1)].filter(Boolean).join('/') : p))
      .filter(Boolean))]);
    setFolderFilter((cur) => (cur !== ALL_FOLDERS && cur !== TOP_LEVEL && isWithin(cur, from)
      ? ([to, cur.slice(from.length + 1)].filter(Boolean).join('/') || TOP_LEVEL) : cur));
    refreshAfterOrganize();
    return before;
  }, [folderCounts, project.id, onNotFound, refreshAfterOrganize, setPendingFolders]);
  // Renaming onto a folder that already exists merges the two: ask first, never do it silently.
  const [treeOpen, setTreeOpen] = useState(false);
  // Everything in the project (the All files count): every folder's own files plus the top level's.
  const projectTotal = rootCount === undefined ? undefined : folderCounts.reduce((a, f) => a + f.count, rootCount);
  const folderLabel = (f: string) => (f === ALL_FOLDERS ? 'All files' : f === TOP_LEVEL ? 'Top level' : folderName(f));
  const [mergeAsk, setMergeAsk] = useState<{ from: string; to: string } | null>(null);
  const renameFolderTo = useCallback(async (from: string, to: string, confirmed = false) => {
    const exists = to !== from && (folderCounts.some((f) => isWithin(f.folder, to)) || pendingFolders.some((p) => isWithin(p, to)));
    if (exists && !confirmed) { setMergeAsk({ from, to }); return; }
    const before = await relocateFolder(from, to);
    if (before?.size) {
      setLastMove({ label: exists ? `Merged ${folderName(from)} into ${folderName(to)}` : `Renamed ${folderName(from)} to ${folderName(to)}`,
        to, from: before });
    }
  }, [folderCounts, pendingFolders, relocateFolder]);
  // Delete folder: nothing is deleted — its files, saved moments and subfolders move up to its parent.
  const [deleteAsk, setDeleteAsk] = useState<string | null>(null);
  const deleteFolder = useCallback(async (folder: string) => {
    setDeleteAsk(null);
    const parent = parentFolder(folder);
    const before = await relocateFolder(folder, parent);
    if (before === null) return;
    setPendingFolders((cur) => cur.filter((p) => p !== parent || folderCounts.some((f) => isWithin(f.folder, p))));
    setFolderFilter(parent || TOP_LEVEL);
    const n = [...before.values()].reduce((a, ids) => a + ids.length, 0);
    setLastMove(n
      ? { label: `Deleted ${folderName(folder)} · ${n.toLocaleString('en-US')} item${n === 1 ? '' : 's'} moved to ${parent ? folderName(parent) : 'Top level'}`,
        to: parent, from: before }
      : null);
  }, [relocateFolder, setPendingFolders, folderCounts]);
  const askDeleteFolder = useCallback((folder: string) => {
    const holds = folderCounts.some((f) => f.count > 0 && isWithin(f.folder, folder));
    // An empty folder made here holds nothing: it goes without a question.
    if (!holds) { void deleteFolder(folder); return; }
    setDeleteAsk(folder);
  }, [folderCounts, deleteFolder]);
  // A drag carries the whole selection when the dragged card is part of it, else just that card.
  const dragIds = useCallback((a: AssetView) => (selected.has(a.assetId) ? [...selected] : [a.assetId]), [selected]);
  const currentFolder = folderFilter === ALL_FOLDERS ? undefined : folderFilter === TOP_LEVEL ? '' : folderFilter;
  // The open folder's saved moments (bookmarks) show after its files; their count keeps "This folder is empty" honest.
  const [momentCount, setMomentCount] = useState(0);
  useEffect(() => { setMomentCount(0); }, [project.id, folderFilter]);

  // `Upload ▾`: Files, Folder (the two hidden pickers), and Import from S3… (the project's import inbox).
  const [uploadMenuOpen, setUploadMenuOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const uploadTriggerRef = useRef<HTMLButtonElement>(null);
  const uploadMenuId = useId();
  const closeUploadMenu = useCallback((refocus: boolean) => {
    setUploadMenuOpen(false);
    if (refocus) uploadTriggerRef.current?.focus();
  }, []);
  const uploadMenu = useRovingMenu({ open: uploadMenuOpen, itemSelector: '[role="menuitem"]', onClose: closeUploadMenu });
  const pickFrom = (input: React.RefObject<HTMLInputElement | null>) => {
    closeUploadMenu(false);
    input.current?.click();
  };

  // Duplicate check before an upload: one prompt for the batch (Use existing / Keep both / Cancel).
  const [pending, setPending] = useState<{ files: File[]; duplicates: Duplicate[] } | null>(null);
  // A large batch into an indexed Project: confirm the analysis cost first.
  const [costConfirm, setCostConfirm] = useState<File[] | null>(null);

  // Indexing ON/OFF, from the host when it knows, else read once from the Project's META when a batch needs it.
  // Absent (an older proxy) or unreadable counts as ON: asking once too often beats analyzing 1,000 files unasked.
  const indexingRead = useRef<Promise<boolean> | null>(null);
  const indexingOn = useCallback((): Promise<boolean> => {
    if (typeof knownIndexing === 'boolean') return Promise.resolve(knownIndexing);
    if (!indexingRead.current) {
      indexingRead.current = getProject(project.id)
        .then((r) => (r.ok ? r.value.project?.indexing !== false : true))
        .catch(() => true);
    }
    return indexingRead.current;
  }, [project.id, knownIndexing]);

  // The search collection's state rides on the asset list (Req 10.12, 10.18); the list keeps polling while a
  // rebuild runs, so the notice clears without a reload.
  const collectionState = assets.collectionState;
  const [rebuildError, setRebuildError] = useState<string | null>(null);
  const retryCollectionRebuild = async () => {
    setRebuildError(null);
    const r = await retryRebuild(project.id);
    if (!r.ok) setRebuildError(`Couldn't retry the rebuild: ${r.kind === 'error' ? r.message : 'try again.'}`);
    void assets.refresh();
  };

  // A different Project starts clean. The first mount keeps `initialFilter` (a project-browser tab's seed): this
  // effect also runs then, and must not wipe what the agent asked to show.
  const seededPid = useRef<string | null>(project.id);
  useEffect(() => {
    const seed = seededPid.current === project.id ? initialFilter : undefined;
    seededPid.current = null;
    setQuery(seed?.q ?? '');
    setKind(FILTER_KINDS.includes(seed?.kind ?? '') ? (seed!.kind as AssetTypeFilter) : '');
    setFolderFilter(seed?.folder || ALL_FOLDERS);
    setToggled(new Set());
    setUploadMenuOpen(false);
    setBulkConfirm(null);
    setBulkFailures([]);
    setPending(null);
    setCostConfirm(null);
    indexingRead.current = null;
    setUploadError(null);
    setOpenError(null);
    setDeleteError(null);
    setConfirm(null);
  }, [project.id]);

  // Each file this tab finishes uploading into the Project shows up in the grid — and once it IS in the
  // grid, its "Uploaded" row is redundant (it read as a second, list-style view of the same files), so
  // it leaves the Uploads list. Failed and canceled rows stay until dismissed: they appear nowhere else.
  // A large batch (the summary row) keeps its finished rows instead, so "312 of 1,000 uploaded" keeps counting,
  // and refreshes the grid at most every few seconds rather than once per file.
  const readyIds = uploads.filter((u) => u.status === 'ready').map((u) => u.id);
  const readyCount = readyIds.length;
  const lastReady = useRef(readyCount);
  const readyIdsRef = useRef(readyIds);
  readyIdsRef.current = readyIds;
  const summarized = uploads.length > UPLOAD_SUMMARY_THRESHOLD;
  const summarizedRef = useRef(summarized);
  summarizedRef.current = summarized;
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const { refresh } = assets;
  useEffect(() => {
    if (readyCount > lastReady.current) {
      const landed = readyIdsRef.current;
      if (summarizedRef.current) {
        if (!refreshTimer.current) {
          refreshTimer.current = setTimeout(() => {
            refreshTimer.current = null;
            void refresh();
          }, LARGE_BATCH_REFRESH_MS);
        }
      } else {
        void refresh().then(() => landed.forEach(dismiss));
      }
    }
    lastReady.current = readyCount;
  }, [readyCount, refresh]);
  useEffect(() => () => {
    if (refreshTimer.current) clearTimeout(refreshTimer.current);
  }, []);

  // Uploads land in the folder that is open (a dropped or picked folder's own tree nests under it); at All files
  // or Top level they land where they always did.
  const uploadFolder = folderFilter !== ALL_FOLDERS && folderFilter !== TOP_LEVEL ? folderFilter : undefined;
  const start = useCallback(
    (files: File[]) => {
      void startBatch(project.id, files, undefined, uploadFolder).then((r) => {
        if (!r.ok) setUploadError(r.message);
        else if (uploadFolder) setFoldersSeq((n) => n + 1);
      });
    },
    [project.id, uploadFolder],
  );

  const send = useCallback(
    (files: File[]) => {
      if (files.length === 0) return;
      if (files.length <= COST_CONFIRM_THRESHOLD) {
        start(files);
        return;
      }
      void indexingOn().then((on) => {
        if (on) setCostConfirm(files);
        else start(files);
      });
    },
    [start, indexingOn],
  );

  const resolveCost = (go: boolean) => {
    const files = costConfirm;
    setCostConfirm(null);
    if (go && files) start(files); // Cancel: nothing from this batch uploads
  };

  const upload = useCallback(
    (files: File[]) => {
      if (files.length === 0) return;
      setUploadError(null);
      // The check never blocks an upload: if it cannot run, the files upload as before.
      void findDuplicates(project.id, files).catch(() => [] as Duplicate[]).then((duplicates) => {
        if (duplicates.length === 0) send(files);
        else setPending({ files, duplicates });
      });
    },
    [project.id, send],
  );

  const resolvePending = (keepBoth: boolean | null) => {
    const p = pending;
    setPending(null);
    if (!p || keepBoth === null) return; // Cancel: nothing from this batch uploads
    const dup = new Set(p.duplicates.map((d) => d.file));
    send(keepBoth ? p.files : p.files.filter((f) => !dup.has(f)));
  };

  const onPick = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    e.target.value = '';
    upload(files);
  };

  // `<input webkitdirectory>`: each File carries `webkitRelativePath` ("Footage/Day 1/a.mp4"). Chrome and Edge show
  // their own "Upload N files to this site?" confirmation for this picker before `change` fires; a page cannot
  // suppress it. A drop never asks.
  const onPickFolder = (e: React.ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(e.target.files ?? []);
    e.target.value = '';
    if (picked.length === 0) return;
    const files = filesFromDirectoryInput(picked);
    if (files.length === 0) {
      setUploadError('That folder has no files to upload.');
      return;
    }
    upload(files);
  };

  const onDragOver = (e: React.DragEvent<HTMLElement>) => {
    if (!dragHasFiles(e.dataTransfer)) return;
    e.preventDefault();
    if (!dragging) setDragging(true);
  };
  const onDragLeave = (e: React.DragEvent<HTMLElement>) => {
    if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
    setDragging(false);
  };
  const onDrop = (e: React.DragEvent<HTMLElement>) => {
    setDragging(false);
    const dt = e.dataTransfer;
    if (!dragHasFiles(dt)) return;
    e.preventDefault();
    // Read the items NOW: the DataTransferItemList is only readable during the event.
    void readDroppedFiles(dt).then((files) => {
      if (files.length === 0) {
        setUploadError('Nothing to upload: the drop held no files.');
        return;
      }
      upload(files);
    });
  };

  const confirmDelete = async () => {
    const target = confirm;
    if (!target) return;
    setConfirm(null);
    setDeleteError(null);
    assets.hide(target.assetId);
    const r = await removeAsset(project.id, target.assetId);
    if (!r.ok) {
      if (r.kind === 'not_found') {
        // Gone already, or the Project is: a refresh settles which.
      } else {
        assets.unhide(target.assetId);
        setDeleteError(`Couldn't delete ${target.displayName}: ${r.kind === 'error' ? r.message : 'try again.'}`);
      }
    }
    void assets.refresh();
    if (searching) setSearchSeq((n) => n + 1);
  };

  // Bulk delete: the same per-asset remove (and so the same permission check) as a single delete, 6 at a time.
  // Every target hides at once; a failure comes back; the grid refreshes once at the end.
  const runBulkDelete = async () => {
    const targets = bulkConfirm;
    setBulkConfirm(null);
    if (!targets || targets.length === 0) return;
    setDeleteError(null);
    setBulkFailures([]);
    setBulkProgress({ done: 0, total: targets.length });
    for (const a of targets) assets.hide(a.assetId);
    const failures = await deleteInParallel(targets, (id) => removeAsset(project.id, id), {
      onSettled: () => setBulkProgress((p) => (p ? { ...p, done: p.done + 1 } : p)),
    });
    for (const f of failures) assets.unhide(f.asset.assetId);
    setSelected(new Set());
    setBulkProgress(null);
    setBulkFailures(failures);
    void assets.refresh();
    if (searching) setSearchSeq((n) => n + 1);
  };

  // Retry a failed asset: it goes back to `queued` and the pipeline reruns only what failed (Req 7.10).
  const retry = async (target: AssetView) => {
    setDeleteError(null);
    const r = await retryAsset(project.id, target.assetId);
    if (!r.ok && r.kind !== 'not_found') {
      setDeleteError(`Couldn't retry ${target.displayName}: ${r.kind === 'error' ? r.message : 'try again.'}`);
    }
    void assets.refresh();
    if (searching) setSearchSeq((n) => n + 1);
  };

  const card = (a: AssetView, nested: boolean) => (
    <AssetCard
      key={a.assetId}
      asset={a}
      projectId={project.id}
      sessionId={sessionId ?? null}
      onDelete={browseOnly ? undefined : setConfirm}
      onRetry={browseOnly ? undefined : retry}
      onOpenError={setOpenError}
      layout={view}
      inFolder={nested}
      shownIn={grouped ? assetFolder(a) : filtering ? (folderFilter === TOP_LEVEL ? '' : folderFilter) : searching ? null : ''}
      selected={selected.has(a.assetId)}
      selecting={selecting}
      onSelectedChange={browseOnly || bulkProgress ? undefined : setOneSelected}
      dragIds={showTree && !bulkProgress ? dragIds : undefined}
      onMove={browseOnly ? undefined : (one) => { moveFromCard.current = true; setSelected(new Set([one.assetId])); setMoveOpen(true); }}
      onLineage={browseOnly || !onShowLineage ? undefined : (one) => onShowLineage(one.assetId)}
    />
  );

  const finished = uploads.some((u) => isTerminal(u.status));
  const showEmpty = !searching && assets.loaded && !assets.error && assets.assets.length === 0;

  return (
    <section
      ref={sectionRef}
      aria-label="Assets"
      data-testid="project-assets"
      data-browse-only={browseOnly ? 'true' : undefined}
      style={dragging ? { ...sectionStyle, ...draggingStyle } : sectionStyle}
      onDragOver={browseOnly ? undefined : onDragOver}
      onDragLeave={browseOnly ? undefined : onDragLeave}
      onDrop={browseOnly ? undefined : onDrop}
    >
      {/* The pickers sit outside the toolbar row so the selection bar can replace that row without unmounting them. */}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        onChange={onPick}
        style={hiddenInputStyle}
        tabIndex={-1}
        aria-hidden="true"
        data-testid="project-upload-input"
      />
      <input
        ref={folderInputRef}
        type="file"
        multiple
        // Non-standard but supported by every current browser; React passes unknown lowercase attributes through.
        {...({ webkitdirectory: '', directory: '' } as React.InputHTMLAttributes<HTMLInputElement>)}
        onChange={onPickFolder}
        style={hiddenInputStyle}
        tabIndex={-1}
        aria-hidden="true"
        data-testid="project-folder-input"
      />
      <div style={showTree ? splitStyle : undefined}>
        {showTree && (
          <FolderTree
            tree={folderTree}
            selected={folderFilter}
            allValue={ALL_FOLDERS}
            topValue={TOP_LEVEL}
            rootCount={rootCount}
            totalCount={projectTotal}
            onDelete={askDeleteFolder}
            onSelect={setFolderFilter}
            onDropAssets={(f, ids) => void moveTo(ids, f)}
            onCreate={createFolder}
            onRename={(from, to) => void renameFolderTo(from, to)}
            onCollapse={() => setTreeCollapsed(true)}
            readOnly={browseOnly}
          />
        )}
        <div style={showTree ? mainColStyle : undefined}>
        {/* One fixed row. Nothing in it depends on the view, so Icons ↔ List never moves a control; it never wraps
            (the search field is the only part that shrinks). While anything is selected the same row, at the same
            height, is the selection bar instead. */}
        {selecting && !browseOnly ? (
          <div role="toolbar" aria-label="Selection" style={toolbarStyle} data-testid="assets-selection-bar">
            {bulkProgress ? (
              <span role="status" style={selectionCountStyle}>{bulkDeleteProgress(bulkProgress.done, bulkProgress.total)}</span>
            ) : (
              <>
                <span style={selectionCountStyle} data-testid="assets-selection-count">
                  {`${selectedOnScreen.length.toLocaleString('en-US')} selected`}
                </span>
                <span style={dotStyle} aria-hidden="true">·</span>
                <button
                  type="button"
                  style={barLinkStyle}
                  onClick={selectAll}
                  disabled={allSelected}
                  data-testid="assets-select-all"
                >
                  Select all
                </button>
                <span style={{ flex: 1 }} />
                <span style={{ position: 'relative', display: 'inline-flex' }}>
                  <Button variant="primary" size="md" style={controlHeight} onClick={() => setMoveOpen((o) => !o)}
                    aria-haspopup="dialog" aria-expanded={moveOpen} data-testid="assets-move">
                    <FolderIcon size={14} />
                    Move to…
                  </Button>
                  {moveOpen && (
                    <FolderPicker
                      folders={allFolderPaths}
                      current={currentFolder}
                      title={`Move ${selectedOnScreen.length.toLocaleString('en-US')} file${selectedOnScreen.length === 1 ? '' : 's'} to`}
                      align="right"
                      onPick={(f) => void moveTo(selectedOnScreen.map((a) => a.assetId), f)}
                      onClose={() => {
                        setMoveOpen(false);
                        // Opened from a card's ⋯: cancelling leaves nothing selected behind (no extra Clear).
                        if (moveFromCard.current) { moveFromCard.current = false; setSelected(new Set()); }
                      }}
                    />
                  )}
                </span>
                <Button variant="danger" size="md" style={controlHeight} onClick={() => setBulkConfirm(selectedOnScreen)}>Delete</Button>
                <Button variant="ghost" size="md" style={controlHeight} onClick={() => setSelected(new Set())}>Clear</Button>
              </>
            )}
          </div>
        ) : (
          <div role="toolbar" aria-label="Assets" style={toolbarStyle} data-testid="assets-toolbar">
            {/* Collapsed by choice with room to dock: the way back sits where the list was. */}
            {roomForTree && treeCollapsed && (
              <button type="button" style={showTreeBtnStyle} onClick={() => setTreeCollapsed(false)}
                aria-label="Show the folder list" title="Show the folder list" data-testid="assets-show-folders">
                <FolderIcon size={14} /><span aria-hidden="true">»</span>
              </button>
            )}
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search by name"
              aria-label="Search files by name"
              style={roomForTree ? searchInputStyle : { ...searchInputStyle, minWidth: 80 }}
              data-testid="assets-search"
            />
            <span style={{ flex: '1 1 0', minWidth: 0 }} aria-hidden="true" />
            {/* Narrow: the SAME tree, in a popover — so New folder, Rename, Delete and drop work at any width. */}
            {!showTree && (
              <span style={{ position: 'relative', display: 'inline-flex' }}>
                <button type="button" style={{ ...folderPickBtnStyle, maxWidth: 140 }} aria-haspopup="dialog" aria-expanded={treeOpen}
                  aria-label={`Folder: ${folderLabel(folderFilter)}`} data-testid="assets-folder-button"
                  onClick={() => setTreeOpen((o) => !o)}>
                  <FolderIcon size={14} />
                  <span style={folderBtnNameStyle}>{folderLabel(folderFilter)}</span>
                  <ChevronDownIcon size={11} />
                </button>
                {treeOpen && (
                  <>
                    <span style={popScrimStyle} aria-hidden="true" onMouseDown={() => setTreeOpen(false)} />
                    <div role="dialog" aria-label="Folders" style={treePopStyle} data-testid="assets-folder-popover">
                      <FolderTree
                        tree={folderTree}
                        selected={folderFilter}
                        allValue={ALL_FOLDERS}
                        topValue={TOP_LEVEL}
                        rootCount={rootCount}
                        totalCount={projectTotal}
                        onSelect={(f) => { setFolderFilter(f); setTreeOpen(false); }}
                        onDropAssets={(f, ids) => { setTreeOpen(false); void moveTo(ids, f); }}
                        onCreate={createFolder}
                        onRename={(from, to) => void renameFolderTo(from, to)}
                        onDelete={browseOnly ? undefined : (f) => { setTreeOpen(false); askDeleteFolder(f); }}
                        onPin={roomForTree ? () => { setTreeOpen(false); setTreeCollapsed(false); } : undefined}
                        readOnly={browseOnly}
                        compact
                      />
                    </div>
                  </>
                )}
              </span>
            )}
            <select
              value={kind}
              onChange={(e) => setKind(e.target.value as AssetTypeFilter)}
              aria-label="File type"
              style={roomForTree ? kindSelectStyle : { ...kindSelectStyle, maxWidth: 104 }}
              data-testid="assets-kind"
            >
              <option value="">All types</option>
              <option value="video">Video</option>
              <option value="image">Image</option>
              <option value="audio">Audio</option>
              <option value="document">Document</option>
              <option value={ANALYSIS_TYPE}>{ANALYSIS_TYPE_LABEL}</option>
              <option value={DATA_TYPE}>{DATA_TYPE_LABEL}</option>
            </select>
            <div role="radiogroup" aria-label="View" style={segmentedStyle} data-testid="assets-view-toggle">
              {([
                ['grid', 'Icons', GridViewIcon],
                ['list', 'List', ListViewIcon],
              ] as const).map(([v, label, Icon]) => (
                <button
                  key={v}
                  type="button"
                  role="radio"
                  aria-checked={view === v}
                  aria-label={`${label} view`}
                  title={`${label} view`}
                  style={view === v ? { ...segmentStyle, ...segmentOnStyle } : segmentStyle}
                  onClick={() => chooseView(v)}
                >
                  <Icon size={14} />
                </button>
              ))}
            </div>
            {!browseOnly && <span style={{ position: 'relative', display: 'inline-flex', flexShrink: 0 }}>
              <Button
                ref={uploadTriggerRef}
                variant="secondary"
                size="md"
                style={controlHeight}
                aria-label={UPLOAD_BUTTON_NAME}
                aria-haspopup="menu"
                aria-expanded={uploadMenuOpen}
                aria-controls={uploadMenuOpen ? uploadMenuId : undefined}
                onClick={() => (uploadMenuOpen ? closeUploadMenu(false) : setUploadMenuOpen(true))}
                data-testid="assets-upload"
              >
                <UploadIcon size={14} />
                {/* Narrow pane: the icon alone, so the one toolbar row still fits (its name is the aria-label). */}
                {roomForTree && UPLOAD_BUTTON_LABEL}
                <ChevronDownIcon size={12} />
              </Button>
              {uploadMenuOpen && (
                <>
                  <span style={scrimStyle} aria-hidden="true" onClick={() => closeUploadMenu(false)} />
                  <div
                    ref={uploadMenu.menuRef}
                    id={uploadMenuId}
                    role="menu"
                    aria-label={UPLOAD_BUTTON_LABEL}
                    style={menuStyle}
                    onKeyDown={uploadMenu.onKeyDown}
                    onFocusCapture={uploadMenu.onFocusCapture}
                  >
                    {/* Where uploads land, so nobody has to guess (the open folder, else the top level). */}
                    <span style={uploadIntoStyle} data-testid="assets-upload-into">
                      Into {uploadFolder ? folderName(uploadFolder) : 'Top level'}
                    </span>
                    {([
                      ['files', UPLOAD_FILES_ITEM, UploadIcon, fileInputRef],
                      ['folder', UPLOAD_FOLDER_ITEM, FolderIcon, folderInputRef],
                    ] as const).map(([key, label, Icon, ref]) => (
                      <button
                        key={key}
                        type="button"
                        role="menuitem"
                        data-menu-key={key}
                        style={uploadMenu.highlight === key ? { ...menuItemStyle, background: theme.colors.activeBg } : menuItemStyle}
                        onMouseEnter={uploadMenu.hoverFocus}
                        onClick={() => pickFrom(ref)}
                      >
                        <Icon size={14} />
                        {label}
                      </button>
                    ))}
                    <button
                      type="button"
                      role="menuitem"
                      data-menu-key="import"
                      style={uploadMenu.highlight === 'import' ? { ...menuItemStyle, background: theme.colors.activeBg } : menuItemStyle}
                      onMouseEnter={uploadMenu.hoverFocus}
                      onClick={() => { closeUploadMenu(false); setImportOpen(true); }}
                      data-testid="assets-import"
                    >
                      <UploadIcon size={14} />
                      {UPLOAD_IMPORT_ITEM}
                    </button>
                  </div>
                </>
              )}
            </span>}
          </div>
        )}

        {dragging && <p style={dropHintStyle}>{`${DROP_HINT} to upload to ${project.name}`}</p>}
        {uploadError && <p style={errorStyle} role="alert">{uploadError}</p>}

        {/* Always mounted: the batch announcement's live region must outlive the rows it summarizes
            (finished uploads leave the list as soon as the grid shows them). */}
        {!browseOnly && <div
          style={uploads.length > 0 ? uploadsStyle : undefined}
          data-testid={uploads.length > 0 ? 'project-uploads' : undefined}
        >
          {uploads.length > 0 && (
            <div style={uploadsHeaderStyle}>
              <span>Uploads</span>
              {finished && (
                <button type="button" style={linkBtnStyle} onClick={() => clearFinished(project.id)}>
                  Clear finished
                </button>
              )}
            </div>
          )}
          <UploadFileList entries={uploads} />
        </div>}

        {Slot ? (
          <Slot {...props} />
        ) : (
          <>
            {organizeError && (
              <p style={errorStyle} role="alert">
                {organizeError}{' '}
                <button type="button" style={linkBtnStyle} onClick={() => setOrganizeError(null)}>Dismiss</button>
              </p>
            )}
            {lastMove && (
              <div role="status" style={undoBarStyle} data-testid="assets-move-status">
                <span>{lastMove.label}</span>
                <button type="button" style={undoBtnStyle} onClick={() => void undoMove()}>Undo</button>
                {lastMove.to !== currentFolder && (
                  <button type="button" style={undoBtnStyle}
                    onClick={() => { setFolderFilter(lastMove.to === '' ? TOP_LEVEL : lastMove.to); setLastMove(null); }}>
                    Open folder
                  </button>
                )}
              </div>
            )}
            {openError && (
              <p style={errorStyle} role="alert">
                {openError}{' '}
                <button type="button" style={linkBtnStyle} onClick={() => setOpenError(null)}>Dismiss</button>
              </p>
            )}
            {collectionState === 'rebuilding' && (
              <p style={noticeStyle} role="status" data-testid="collection-rebuilding">
                Search is being rebuilt. Results can be partial until it finishes.
              </p>
            )}
            {collectionState === 'rebuild_failed' && !browseOnly && (
              <p style={errorStyle} role="alert" data-testid="collection-rebuild-failed">
                Rebuilding this project's search failed.{' '}
                <button type="button" style={inlineLinkStyle} onClick={() => void retryCollectionRebuild()}>
                  Retry rebuild
                </button>
              </p>
            )}
            {rebuildError && <p style={errorStyle} role="alert">{rebuildError}</p>}
            {deleteError && <p style={errorStyle} role="alert">{deleteError}</p>}
            {bulkFailures.length > 0 && (
              <div style={errorStyle} role="alert" data-testid="bulk-delete-failures">
                {`Couldn't delete ${bulkFailures.length.toLocaleString('en-US')} file${bulkFailures.length === 1 ? '' : 's'}:`}{' '}
                <button type="button" style={linkBtnStyle} onClick={() => setBulkFailures([])}>Dismiss</button>
                <ul style={failureListStyle}>
                  {bulkFailures.map((f) => (
                    <li key={f.asset.assetId}>{`${f.asset.displayName}: ${f.message}`}</li>
                  ))}
                </ul>
              </div>
            )}
            {assets.error && (
              <p style={errorStyle} role="alert">
                {assets.error}{' '}
                <button type="button" style={linkBtnStyle} onClick={() => void assets.reload()}>Try again</button>
              </p>
            )}
            {!assets.loaded && <p style={mutedStyle}>Loading assets…</p>}
            {showEmpty && (
              <div style={emptyStyle} data-testid="project-assets-empty">
                <span style={emptyIconStyle}><FolderIcon size={28} /></span>
                <p style={emptyTitleStyle}>{ASSETS_EMPTY_TITLE}</p>
                <p style={emptyBodyStyle}>{browseOnly ? ASSETS_EMPTY_BODY_BROWSE : ASSETS_EMPTY_BODY}</p>
              </div>
            )}
            {searching && searchError && <p style={errorStyle} role="alert">{searchError}</p>}
            {searching && found && found.assets.length === 0 && (query.trim() !== '' || serverKind !== '') && (
              <p style={mutedStyle} data-testid="assets-search-empty">No files match.</p>
            )}
            {subfolders.length > 0 && query.trim() === '' && (
              <ul style={subfolderGridStyle} aria-label="Folders here" data-testid="assets-subfolders">
                {subfolders.map((n) => (
                  <li key={n.path} style={{ listStyle: 'none' }}>
                    <button type="button" data-testid="assets-subfolder" data-folder={n.path}
                      style={dropOn === n.path ? { ...subfolderStyle, ...subfolderDropStyle } : subfolderStyle}
                      onClick={() => setFolderFilter(n.path)}
                      onDragOver={browseOnly ? undefined : (e) => {
                        if (!isAssetDrag(e.dataTransfer)) return;
                        e.preventDefault(); e.dataTransfer.dropEffect = 'move'; setDropOn(n.path);
                      }}
                      onDragLeave={() => setDropOn((c) => (c === n.path ? null : c))}
                      onDrop={browseOnly ? undefined : (e) => {
                        const ids = draggedAssetIds(e.dataTransfer);
                        setDropOn(null);
                        if (!ids?.length) return;
                        e.preventDefault();
                        void moveTo(ids, n.path);
                      }}>
                      <FolderIcon size={16} />
                      <span style={subfolderNameStyle}>{n.name}</span>
                      {n.total > 0 && <span style={subfolderCountStyle}>{n.total.toLocaleString('en-US')}</span>}
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {searching && found && found.assets.length === 0 && query.trim() === '' && serverKind === '' && momentCount === 0
              && subfolders.length === 0 && (
              <div style={emptyStyle} data-testid="assets-folder-empty">
                <span style={emptyIconStyle}><FolderIcon size={28} /></span>
                <p style={emptyTitleStyle}>This folder is empty</p>
                <p style={emptyBodyStyle}>{browseOnly ? 'No files are filed here.'
                  : 'Upload here, or drag files onto it in the folder list.'}</p>
              </div>
            )}
            {searching && found?.cursor && (
              <p style={mutedStyle} data-testid="assets-search-more">
                {typeof found.total === 'number'
                  ? `Showing ${found.assets.length} of ${found.total} matches, best match first.`
                  : 'Best match first.'}{' '}
                <button type="button" style={linkBtnStyle} onClick={loadMoreSearch} disabled={loadingMore}>
                  {loadingMore ? 'Loading…' : 'Load more'}
                </button>
              </p>
            )}
            {searching && found && !found.cursor && found.truncated && (
              <p style={mutedStyle}>This project is large; some matches beyond the first results may not be shown. Type more of the name to narrow it down.</p>
            )}
            {!showAnalysis && !showData && inFolder.length > 0 && shown.length === 0 && hiddenCount > 0 && (
              <p style={mutedStyle} data-testid="assets-analysis-hidden">{ANALYSIS_HIDDEN_HINT}</p>
            )}
            {showAnalysis && inFolder.length > 0 && shown.length === 0 && (
              <p style={mutedStyle} data-testid="assets-analysis-empty">No analysis files here.</p>
            )}
            {showData && inFolder.length > 0 && shown.length === 0 && (
              <p style={mutedStyle} data-testid="assets-metadata-empty">No metadata files here.</p>
            )}
            {filtering && !searching && listed.length > 0 && inFolder.length === 0 && (
              <p style={mutedStyle} data-testid="assets-folder-empty">
                {assets.cursor && !searching ? 'No files in this folder among those loaded.' : 'No files in this folder.'}
              </p>
            )}
            {shown.length > 0 && view === 'list' && (
              <AssetListHeader sort={sort} onSort={onSort}
                withCheckbox={!browseOnly && !bulkProgress} withMenu={!browseOnly} />
            )}
            {shown.length > 0 && (
              <ul
                style={view === 'list' ? listViewStyle : gridStyle}
                aria-label={`${project.name} assets`}
                data-testid="project-assets-grid"
                data-view={view}
              >
                {groups
                  ? groups.map((g) => {
                    if (g.folder === '') return g.assets.map((a) => card(a, false));
                    const open = folderOpen(g.folder);
                    return (
                      <React.Fragment key={`folder:${g.folder}`}>
                        <li style={folderRowStyle} data-testid="asset-folder-row" data-folder={g.folder}>
                          <button
                            type="button"
                            style={folderBtnStyle}
                            aria-expanded={open}
                            aria-label={`${g.folder}, ${g.assets.length} file${g.assets.length === 1 ? '' : 's'}`}
                            onClick={() => toggleFolder(g.folder)}
                          >
                            <span style={open ? chevronStyle : { ...chevronStyle, transform: 'rotate(-90deg)' }}>
                              <ChevronDownIcon size={12} />
                            </span>
                            <span style={folderIconStyle}><FolderIcon size={16} /></span>
                            <span style={folderPathStyle} title={g.folder}>{g.folder}</span>
                            <span style={folderCountStyle}>{g.assets.length.toLocaleString('en-US')}</span>
                          </button>
                        </li>
                        {open && g.assets.map((a) => card(a, true))}
                      </React.Fragment>
                    );
                  })
                  : ordered.map((a) => card(a, false))}
              </ul>
            )}
            {currentFolder !== undefined && query.trim() === '' && serverKind === '' && (
            <SavedMoments projectId={project.id} folder={currentFolder} sessionId={sessionId ?? null}
              refreshKey={foldersSeq} onCount={setMomentCount} readOnly={browseOnly}
              folders={allFolderPaths} onMoved={() => setFoldersSeq((n) => n + 1)} />
          )}
          {!searching && assets.cursor && (
              <div style={moreRowStyle}>
                <Button variant="secondary" size="md" onClick={() => void assets.loadMore()} disabled={assets.loading}>
                  {assets.loading ? 'Loading…' : 'Load more'}
                </Button>
              </div>
            )}
          </>
        )}
        </div>
      </div>

      {!browseOnly && importOpen && (
        <ImportPanel projectId={project.id} open={importOpen} onClose={() => { setImportOpen(false); refreshAfterOrganize(); }} />
      )}
      <DuplicatesDialog
        duplicates={pending?.duplicates ?? null}
        batchSize={pending?.files.length ?? 0}
        onUseExisting={() => resolvePending(false)}
        onKeepBoth={() => resolvePending(true)}
        onCancel={() => resolvePending(null)}
      />
      <ConfirmDialog
        open={!!costConfirm}
        title="Analyze these files?"
        message={costConfirm ? costConfirmMessage(costConfirm.length) : ''}
        confirmLabel="Continue"
        onConfirm={() => resolveCost(true)}
        onCancel={() => resolveCost(false)}
      />
      <ConfirmDialog
        open={!!bulkConfirm}
        title={bulkDeleteTitle(bulkConfirm?.length ?? 0)}
        message={BULK_DELETE_MESSAGE}
        confirmLabel="Delete"
        onConfirm={() => void runBulkDelete()}
        onCancel={() => setBulkConfirm(null)}
      >
        {bulkConfirm && (() => {
          const { named, more } = bulkDeleteNames(bulkConfirm.map((a) => a.displayName));
          return (
            <ul style={bulkNamesStyle} data-testid="bulk-delete-names">
              {named.map((n, i) => <li key={`${i}:${n}`}>{n}</li>)}
              {more > 0 && <li>{`+ ${more.toLocaleString('en-US')} more`}</li>}
            </ul>
          );
        })()}
      </ConfirmDialog>
      <ConfirmDialog
        open={!!deleteAsk}
        title={deleteAsk ? `Delete the folder “${folderName(deleteAsk)}”?` : ''}
        message={deleteAsk ? deleteFolderMessage(deleteAsk, folderTree) : ''}
        confirmLabel="Delete folder"
        onConfirm={() => { if (deleteAsk) void deleteFolder(deleteAsk); }}
        onCancel={() => setDeleteAsk(null)}
      />
      <ConfirmDialog
        open={!!mergeAsk}
        title={mergeAsk ? `“${folderName(mergeAsk.to)}” already exists` : ''}
        message={mergeAsk ? `Merge ${folderName(mergeAsk.from)} into it? Their files will be in one folder. You can undo this.` : ''}
        confirmLabel="Merge"
        confirmVariant="primary"
        onConfirm={() => { const m = mergeAsk; setMergeAsk(null); if (m) void renameFolderTo(m.from, m.to, true); }}
        onCancel={() => setMergeAsk(null)}
      />
      <ConfirmDialog
        open={!!confirm}
        title="Delete file"
        message={confirm ? deleteAssetMessage(confirm.displayName) : ''}
        confirmLabel="Delete"
        onConfirm={() => void confirmDelete()}
        onCancel={() => setConfirm(null)}
      />
    </section>
  );
}

/** Section heading, shared by every section of the project body. */
export const projectSectionHeadingStyle: React.CSSProperties = {
  margin: '0 0 12px',
  fontSize: 'var(--fs-sm)',
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textPrimary,
};
/** Every control in the toolbar row is this tall, so the selection bar that replaces the row is too. */
const CONTROL_HEIGHT = 30;
const controlHeight: React.CSSProperties = { height: CONTROL_HEIGHT, boxSizing: 'border-box', flexShrink: 0 };
// The search field is the ONLY toolbar item that shrinks (down to its min width); everything else keeps its size.
const searchInputStyle: React.CSSProperties = {
  flex: '0 1 320px',
  minWidth: 120,
  height: CONTROL_HEIGHT,
  boxSizing: 'border-box',
  padding: '0 8px',
  fontSize: 'var(--fs-sm)',
  color: theme.colors.textPrimary,
  backgroundColor: theme.colors.background,
  border: `1px solid ${theme.colors.borderStrong}`,
  borderRadius: theme.radius.button,
  fontFamily: 'inherit',
};
const kindSelectStyle: React.CSSProperties = {
  ...searchInputStyle,
  flex: '0 0 auto',
  minWidth: 0,
  width: 'auto',
  padding: '0 4px 0 8px',
  cursor: 'pointer',
};
const folderSelectStyle: React.CSSProperties = { ...kindSelectStyle, maxWidth: 180 };
const LARGE_BATCH_REFRESH_MS = 3000;
const selectionCountStyle: React.CSSProperties = {
  fontSize: 'var(--fs-sm)',
  color: theme.colors.textSecondary,
  fontVariantNumeric: 'tabular-nums',
};
const failureListStyle: React.CSSProperties = { margin: '4px 0 0', paddingLeft: 18 };
const bulkNamesStyle: React.CSSProperties = {
  margin: '8px 0 0',
  paddingLeft: 18,
  fontSize: 'var(--fs-sm)',
  color: theme.colors.textPrimary,
  overflowWrap: 'anywhere',
};
const folderRowStyle: React.CSSProperties = { borderBottom: `1px solid ${theme.colors.border}` };
const folderBtnStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  width: '100%',
  padding: '6px 6px 6px 4px',
  margin: 0,
  border: 'none',
  background: theme.colors.surfaceSubtle,
  textAlign: 'left',
  cursor: 'pointer',
  color: theme.colors.textPrimary,
  fontFamily: 'inherit',
  fontSize: 'var(--fs-sm)',
  fontWeight: theme.typography.weights.medium,
};
const chevronStyle: React.CSSProperties = {
  display: 'inline-flex',
  color: theme.colors.textSecondary,
  transition: `transform ${theme.transitions.hover}`,
};
const folderIconStyle: React.CSSProperties = { display: 'inline-flex', color: theme.colors.textSecondary };
const folderPathStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
};
const folderCountStyle: React.CSSProperties = {
  fontSize: 'var(--fs-xs)',
  color: theme.colors.textSecondary,
  fontVariantNumeric: 'tabular-nums',
};

const sectionStyle: React.CSSProperties = {
  position: 'relative',
  borderRadius: theme.radius.panel,
  outline: '2px dashed transparent',
  outlineOffset: 4,
};
const draggingStyle: React.CSSProperties = { outlineColor: theme.colors.primary };
/** Below this section width the folder tree moves into a popover behind a Folder button (the grid needs the room). */
export const TREE_MIN_WIDTH = 760;
const TREE_COLLAPSED_KEY = 'assets-folders-collapsed';
const showTreeBtnStyle: React.CSSProperties = {
  height: CONTROL_HEIGHT, flexShrink: 0, display: 'inline-flex', alignItems: 'center', gap: 4, padding: '0 9px', boxSizing: 'border-box',
  border: `1px solid ${theme.colors.borderStrong}`, borderRadius: theme.radius.button, background: theme.colors.surface,
  color: theme.colors.textSecondary, cursor: 'pointer', fontSize: 'var(--fs-sm)',
};
const uploadIntoStyle: React.CSSProperties = {
  display: 'block', padding: '4px 10px 6px', fontSize: 'var(--fs-xs)', color: theme.colors.textTertiary,
  whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: 220,
};
const subfolderGridStyle: React.CSSProperties = {
  margin: '0 0 14px', padding: 0, display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(170px, 1fr))', gap: 8,
};
const subfolderStyle: React.CSSProperties = {
  width: '100%', display: 'flex', alignItems: 'center', gap: 8, padding: '9px 12px', boxSizing: 'border-box',
  border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.panel, background: theme.colors.surface,
  color: theme.colors.textSecondary, font: 'inherit', fontSize: 'var(--fs-sm)', textAlign: 'left', cursor: 'pointer',
};
const subfolderDropStyle: React.CSSProperties = { borderColor: theme.colors.primary, background: theme.colors.activeBg };
const subfolderNameStyle: React.CSSProperties = {
  flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: theme.colors.textPrimary, fontWeight: 600,
};
const subfolderCountStyle: React.CSSProperties = { fontSize: 'var(--fs-xs)', color: theme.colors.textTertiary };
const folderPickBtnStyle: React.CSSProperties = {
  ...kindSelectStyle, display: 'inline-flex', alignItems: 'center', gap: 6, cursor: 'pointer', maxWidth: 220,
};
const folderBtnNameStyle: React.CSSProperties = { maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' };
const popScrimStyle: React.CSSProperties = { position: 'fixed', inset: 0, zIndex: theme.zIndex.popover };
const treePopStyle: React.CSSProperties = {
  position: 'absolute', right: 0, top: 'calc(100% + 4px)', zIndex: theme.zIndex.popover + 1, width: 280, padding: '8px 0 8px 8px',
  background: theme.colors.surfaceRaised, border: `1px solid ${theme.colors.border}`, borderRadius: theme.radius.panel,
  boxShadow: theme.elevation.elevated,
};

/** The element's content width, live (`null` until measured, or where ResizeObserver is missing, e.g. jsdom). */
function useElementWidth(ref: React.RefObject<HTMLElement | null>): number | null {
  const [w, setW] = useState<number | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver((entries) => setW(entries[0]?.contentRect.width ?? null));
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return w;
}

const splitStyle: React.CSSProperties = { display: 'flex', alignItems: 'stretch', gap: 16, minHeight: 0 };
const mainColStyle: React.CSSProperties = { flex: 1, minWidth: 0 };
const undoBarStyle: React.CSSProperties = {
  display: 'flex', alignItems: 'center', gap: 12, margin: '0 0 10px', padding: '8px 12px',
  borderRadius: theme.radius.panel, background: theme.colors.surfaceSubtle, border: `1px solid ${theme.colors.border}`,
  fontSize: 'var(--fs-sm)', color: theme.colors.textPrimary,
};
const undoBtnStyle: React.CSSProperties = {
  border: 'none', background: 'none', padding: 0, color: theme.colors.primary, font: 'inherit', fontWeight: 600,
  cursor: 'pointer',
};
// Pinned while the grid scrolls: the selection bar (Move to…, Delete) replaces this row, and a long project put it
// out of sight with files still ticked.
const toolbarStyle: React.CSSProperties = {
  display: 'flex',
  flexWrap: 'nowrap',
  alignItems: 'center',
  gap: 8,
  height: CONTROL_HEIGHT + 2,
  marginBottom: 12,
  minWidth: 0,
  position: 'sticky',
  top: 0,
  zIndex: theme.zIndex.sticky,
  background: theme.colors.surface,
  boxShadow: `0 6px 0 ${theme.colors.surface}`,
};

const dotStyle: React.CSSProperties = { color: theme.colors.textTertiary, fontSize: 'var(--fs-sm)' };
const barLinkStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  padding: 0,
  color: theme.colors.primary,
  cursor: 'pointer',
  fontFamily: 'inherit',
  fontSize: 'var(--fs-sm)',
};
const scrimStyle: React.CSSProperties = { position: 'fixed', inset: 0, zIndex: theme.zIndex.popover };
const menuStyle: React.CSSProperties = {
  position: 'absolute',
  top: 'calc(100% + 4px)',
  right: 0,
  zIndex: theme.zIndex.popover + 1,
  minWidth: 140,
  boxSizing: 'border-box',
  background: theme.colors.surfaceRaised,
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  boxShadow: theme.elevation.elevated,
  padding: 4,
};
const menuItemStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  width: '100%',
  padding: '6px 8px',
  background: 'none',
  border: 'none',
  borderRadius: theme.radius.button,
  textAlign: 'left',
  cursor: 'pointer',
  color: theme.colors.textPrimary,
  fontFamily: 'inherit',
  fontSize: 'var(--fs-sm)',
};
const hiddenInputStyle: React.CSSProperties = {
  position: 'absolute',
  width: 1,
  height: 1,
  margin: -1,
  padding: 0,
  border: 0,
  overflow: 'hidden',
  clip: 'rect(0 0 0 0)',
  whiteSpace: 'nowrap',
  opacity: 0,
};
const dropHintStyle: React.CSSProperties = {
  margin: '0 0 12px',
  fontSize: 'var(--fs-sm)',
  color: theme.colors.primary,
};
const errorStyle: React.CSSProperties = { margin: '0 0 12px', fontSize: 'var(--fs-sm)', color: theme.colors.error };
const noticeStyle: React.CSSProperties = { margin: '0 0 12px', fontSize: 'var(--fs-sm)', color: theme.colors.textSecondary };
const inlineLinkStyle: React.CSSProperties = {
  background: 'none', border: 'none', padding: 0, font: 'inherit', color: theme.colors.primary,
  textDecoration: 'underline', cursor: 'pointer',
};
const mutedStyle: React.CSSProperties = { margin: '0 0 12px', fontSize: 'var(--fs-sm)', color: theme.colors.textSecondary };
const uploadsStyle: React.CSSProperties = { marginBottom: 16 };
const uploadsHeaderStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  marginBottom: 6,
  fontSize: 'var(--fs-xs)',
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textSecondary,
};
const linkBtnStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  padding: 0,
  color: theme.colors.primary,
  cursor: 'pointer',
  fontFamily: 'inherit',
  fontSize: 'inherit',
};
const gridStyle: React.CSSProperties = {
  listStyle: 'none',
  margin: 0,
  padding: 0,
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))',
  gap: 12,
};
const moreRowStyle: React.CSSProperties = { display: 'flex', justifyContent: 'center', marginTop: 16 };
const listViewStyle: React.CSSProperties = {
  listStyle: 'none',
  margin: 0,
  padding: 0,
  display: 'flex',
  flexDirection: 'column',
  borderTop: `1px solid ${theme.colors.border}`,
};
// macOS-style segmented control: one bordered pill, the chosen segment raised.
const segmentedStyle: React.CSSProperties = {
  display: 'inline-flex',
  flexShrink: 0,
  padding: 2,
  gap: 2,
  border: `1px solid ${theme.colors.border}`,
  borderRadius: theme.radius.button,
  background: theme.colors.surfaceSubtle,
};
const segmentStyle: React.CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  width: 28,
  height: 24,
  padding: 0,
  border: 'none',
  borderRadius: theme.radius.button,
  background: 'none',
  color: theme.colors.textSecondary,
  cursor: 'pointer',
};
const segmentOnStyle: React.CSSProperties = {
  background: theme.colors.surface,
  color: theme.colors.textPrimary,
  boxShadow: theme.elevation.subtle,
};

// Same metrics as the Agents surface's empty inventory (`caEmptyStyle`).
const emptyStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  justifyContent: 'center',
  textAlign: 'center',
  padding: '48px 24px',
  border: `1px dashed ${theme.colors.border}`,
  borderRadius: theme.radius.panel,
  color: theme.colors.textSecondary,
};
const emptyIconStyle: React.CSSProperties = { display: 'inline-flex', color: theme.colors.textTertiary, marginBottom: 12 };
const emptyTitleStyle: React.CSSProperties = {
  margin: 0,
  fontSize: 'var(--fs-md)',
  fontWeight: theme.typography.weights.semibold,
  color: theme.colors.textSecondary,
};
const emptyBodyStyle: React.CSSProperties = {
  margin: '6px 0 0',
  maxWidth: 420,
  fontSize: 'var(--fs-sm)',
  color: theme.colors.textSecondary,
  lineHeight: 1.5,
};
