import { createContext, useContext, useEffect, useLayoutEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { Monitor, Moon, Sun } from 'lucide-react';

export type ThemePreference = 'dark' | 'light' | 'system';
export type ResolvedTheme = 'dark' | 'light';
export const THEME_KEY = 'stratum-appearance';
export function resolveTheme(preference: ThemePreference, systemDark: boolean): ResolvedTheme {
  return preference === 'system' ? (systemDark ? 'dark' : 'light') : preference;
}
export function readTheme(): ThemePreference {
  try {
    const value = window.localStorage.getItem(THEME_KEY);
    return value === 'light' || value === 'system' ? value : 'dark';
  } catch {
    return 'dark';
  }
}
const ThemeContext = createContext<{
  preference: ThemePreference;
  resolved: ResolvedTheme;
  setPreference: (preference: ThemePreference) => void;
}>({ preference: 'dark', resolved: 'dark', setPreference: () => {} });

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [preference, setPreferenceState] = useState<ThemePreference>(readTheme);
  const [systemDark, setSystemDark] = useState(
    () => window.matchMedia('(prefers-color-scheme: dark)').matches,
  );
  const resolved = resolveTheme(preference, systemDark);
  useLayoutEffect(() => {
    document.documentElement.dataset.theme = resolved;
    document.documentElement.style.colorScheme = resolved;
    document
      .querySelector('meta[name="theme-color"]')
      ?.setAttribute('content', resolved === 'dark' ? '#10151f' : '#f5f7fb');
  }, [resolved]);
  useEffect(() => {
    const query = window.matchMedia('(prefers-color-scheme: dark)');
    const update = () => setSystemDark(query.matches);
    const synchronize = (event: StorageEvent) => {
      if (event.key === THEME_KEY || event.key === null) setPreferenceState(readTheme());
    };
    query.addEventListener('change', update);
    window.addEventListener('storage', synchronize);
    return () => {
      query.removeEventListener('change', update);
      window.removeEventListener('storage', synchronize);
    };
  }, []);
  function setPreference(value: ThemePreference) {
    setPreferenceState(value);
    try {
      window.localStorage.setItem(THEME_KEY, value);
    } catch {
      /* The theme still works when browser storage is unavailable. */
    }
  }
  return (
    <ThemeContext.Provider value={{ preference, resolved, setPreference }}>
      {children}
    </ThemeContext.Provider>
  );
}
export const useTheme = () => useContext(ThemeContext);

export function ThemeControl() {
  const { preference, setPreference } = useTheme();
  return (
    <div className="theme-control" role="group" aria-label="Color theme">
      {(
        [
          { value: 'light', label: 'Use light theme', Icon: Sun },
          { value: 'dark', label: 'Use dark theme', Icon: Moon },
          { value: 'system', label: 'Use system theme', Icon: Monitor },
        ] as const
      ).map(({ value, label, Icon }) => (
        <button
          type="button"
          key={value}
          aria-label={label}
          title={label}
          aria-pressed={preference === value}
          onClick={() => setPreference(value)}
        >
          <Icon size={15} />
        </button>
      ))}
    </div>
  );
}
