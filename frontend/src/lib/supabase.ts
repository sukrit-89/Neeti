import { createClient } from '@supabase/supabase-js';
import type { Session } from '@supabase/supabase-js';

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL;
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY;

if (!supabaseUrl || !supabaseAnonKey) {
    throw new Error('Missing Supabase environment variables');
}

export const supabase = createClient(supabaseUrl, supabaseAnonKey, {
    auth: {
        autoRefreshToken: true,
        persistSession: true,
        detectSessionInUrl: true,
    },
});

let cachedSession: Session | null = null;
let inflightGetSession: Promise<Session | null> | null = null;
let inflightRefreshSession: Promise<Session | null> | null = null;

function isNavigatorLockTimeout(error: unknown): boolean {
    const message = error instanceof Error ? error.message : String(error ?? '');
    return (
        message.includes('NavigatorLockAcquireTimeoutError')
        || message.includes('timed out waiting')
        || message.includes('LockManager lock')
    );
}

function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

async function getSessionWithRetries(retries = 2): Promise<Session | null> {
    let lastError: unknown;

    for (let attempt = 0; attempt <= retries; attempt += 1) {
        try {
            const { data, error } = await supabase.auth.getSession();
            if (error) {
                throw error;
            }
            cachedSession = data.session ?? null;
            return cachedSession;
        } catch (error) {
            lastError = error;
            if (!isNavigatorLockTimeout(error) || attempt === retries) {
                break;
            }
            await sleep(150 * (attempt + 1));
        }
    }

    throw lastError;
}

export async function getSessionSafe(): Promise<Session | null> {
    if (inflightGetSession) {
        return inflightGetSession;
    }

    inflightGetSession = getSessionWithRetries()
        .catch((error) => {
            if (isNavigatorLockTimeout(error) && cachedSession) {
                return cachedSession;
            }
            throw error;
        })
        .finally(() => {
            inflightGetSession = null;
        });

    return inflightGetSession;
}

export async function getAccessTokenSafe(): Promise<string | null> {
    if (cachedSession?.access_token) {
        return cachedSession.access_token;
    }
    const session = await getSessionSafe();
    return session?.access_token ?? null;
}

export async function refreshSessionSafe(): Promise<Session | null> {
    if (inflightRefreshSession) {
        return inflightRefreshSession;
    }

    inflightRefreshSession = (async () => {
        const { data, error } = await supabase.auth.refreshSession();
        if (error) {
            throw error;
        }
        cachedSession = data.session ?? null;
        return cachedSession;
    })().finally(() => {
        inflightRefreshSession = null;
    });

    return inflightRefreshSession;
}

supabase.auth.onAuthStateChange((_event, session) => {
    cachedSession = session ?? null;
});

// Synced for GitHub timestamp

 
