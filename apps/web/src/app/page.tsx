'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/auth';

export default function Home() {
  const { account, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (loading) return;
    router.replace(account ? '/patients' : '/login');
  }, [account, loading, router]);

  return (
    <main className="grid min-h-screen place-items-center">
      <div className="text-center">
        <div className="mx-auto mb-3 h-8 w-8 animate-spin rounded-full border-2 border-[#2A2A2D] border-t-[#F0B429]" />
        <p className="text-sm text-[#9CA3AF]">Loading…</p>
      </div>
    </main>
  );
}
