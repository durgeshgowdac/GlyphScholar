import { redirect } from 'next/navigation'
import { LandingPage } from '@/components/landing-page'

export const dynamic = 'force-dynamic'

export default async function Home({
  searchParams
}: {
  searchParams: Promise<Record<string, string>>
}) {
  const params = await searchParams;
  const code = params?.code;
  if (code) {
    redirect(`/auth/update-password?code=${code}`);
  }
  return <LandingPage />
}