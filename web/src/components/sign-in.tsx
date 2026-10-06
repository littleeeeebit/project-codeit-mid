"use client";

import { createContext, useContext, useEffect, useState } from "react";
import { api, bindAccount, errorText, type Schemas } from "@/lib/api";
import { buttonVariants } from "@/components/ui/button";

type Me = Schemas["Me"];

const MeContext = createContext<Me | null>(null);

/** The signed-in JupyterHub user. Only screens inside <SignedIn> call this. */
export function useMe(): Me {
  const me = useContext(MeContext);
  if (!me) throw new Error("useMe outside <SignedIn>");
  return me;
}

/** Renders the app only for a signed-in member, otherwise the JupyterHub sign-in entry. */
export function SignedIn({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<{ me?: Me; out?: boolean; problem?: string }>({});
  useEffect(() => {
    api.GET("/api/auth/me").then(({ data, error, response }) => {
      if (data) {
        bindAccount(data.name);  // before any screen inside renders and calls
        setState({ me: data });
      }
      else setState(response.status === 401 ? { out: true } : { problem: errorText(error) });
    }).catch(() => setState({ problem: errorText(null) }));
  }, []);
  if (state.me) return <MeContext.Provider value={state.me}>{children}</MeContext.Provider>;
  if (!state.out && !state.problem) return null;
  return (
    <main className="mx-auto flex w-full max-w-md flex-1 flex-col justify-center gap-6 px-6 py-16">
      <h1 className="text-3xl font-bold tracking-tight">입찰메이트</h1>
      {state.problem
        ? <p role="alert" className="text-base text-destructive">{state.problem}</p>
        : <>
            <p className="text-base text-muted-foreground">팀 JupyterHub(:8000) 계정으로 로그인합니다. 비밀번호는 JupyterHub에만 입력되고 입찰메이트에는 전달되지 않습니다.</p>
            <a href="/api/auth/login" className={buttonVariants({ size: "lg" })}>JupyterHub 계정으로 로그인</a>
          </>}
    </main>
  );
}

/** The header's account control: who is signed in, and signing out. */
export function Account() {
  const me = useMe();
  const signOut = async () => {
    await api.POST("/api/auth/logout").catch(() => undefined);
    window.location.reload();  // the page asks who is signed in again and shows the sign-in entry
  };
  return (
    <div className="flex items-center gap-2 text-sm">
      <span className="font-medium" title="JupyterHub 계정">{me.name}</span>
      {!me.local && (
        <button type="button" onClick={signOut}
                className="h-8 rounded-md px-2 text-muted-foreground outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
          로그아웃
        </button>
      )}
    </div>
  );
}
