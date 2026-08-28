import { createContext, useContext, useEffect, useMemo, useReducer, type ReactNode } from 'react'
import { migrateSessionToUser, writeUserJson } from '../shared/storage/userStorage'

interface UiSlice {
  sidebarOpen: boolean
  uploadHistoryExpanded: boolean
}

interface UploadSlice {
  activeTaskId: string | null
}

interface AppStore {
  ui: UiSlice
  upload: UploadSlice
}

type Action =
  | { type: 'toggle-sidebar-open'; open?: boolean }
  | { type: 'toggle-upload-history'; open?: boolean }
  | { type: 'set-upload-task'; taskId: string | null }

const STORE_KEY = 'react_asr_upload_ui_state'

const defaultStore: AppStore = {
  ui: { sidebarOpen: false, uploadHistoryExpanded: false },
  upload: { activeTaskId: null },
}

function loadInitialStore(): AppStore {
  const legacyKey = 'react_asr_ui_state'
  const loaded =
    migrateSessionToUser<AppStore & { ui: UiSlice & { sidebarPinned?: boolean; activeTab?: string } }>(
      STORE_KEY,
      STORE_KEY,
    ) ??
    migrateSessionToUser<AppStore & { ui: UiSlice & { sidebarPinned?: boolean; activeTab?: string } }>(
      legacyKey,
      STORE_KEY,
    ) ??
    defaultStore
  const legacyUi = loaded.ui as UiSlice & { sidebarPinned?: boolean }
  const legacySidebarOpen = legacyUi.sidebarOpen ?? legacyUi.sidebarPinned ?? false
  return {
    upload: {
      activeTaskId: loaded.upload?.activeTaskId ?? null,
    },
    ui: {
      sidebarOpen: legacySidebarOpen,
      uploadHistoryExpanded: legacyUi.uploadHistoryExpanded ?? false,
    },
  }
}

function reducer(state: AppStore, action: Action): AppStore {
  if (action.type === 'toggle-sidebar-open') {
    return {
      ...state,
      ui: { ...state.ui, sidebarOpen: action.open ?? !state.ui.sidebarOpen },
    }
  }
  if (action.type === 'toggle-upload-history') {
    return {
      ...state,
      ui: {
        ...state.ui,
        uploadHistoryExpanded: action.open ?? !state.ui.uploadHistoryExpanded,
      },
    }
  }
  if (action.type === 'set-upload-task') return { ...state, upload: { activeTaskId: action.taskId } }
  return state
}

interface AppStateContextValue {
  store: AppStore
  dispatch: React.Dispatch<Action>
}

const AppStateContext = createContext<AppStateContextValue | null>(null)

export function AppStateProvider({ children }: { children: ReactNode }) {
  const [store, dispatch] = useReducer(reducer, undefined, loadInitialStore)

  useEffect(() => {
    writeUserJson(STORE_KEY, store)
  }, [store])

  const value = useMemo(() => ({ store, dispatch }), [store])

  return <AppStateContext.Provider value={value}>{children}</AppStateContext.Provider>
}

export function useAppState() {
  const ctx = useContext(AppStateContext)
  if (!ctx) throw new Error('useAppState must be used in AppStateProvider')
  return ctx
}
