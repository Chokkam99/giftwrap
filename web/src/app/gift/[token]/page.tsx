import { GiftClient } from "./gift-client";

// Next 16: `params` is a Promise. Synchronous access was removed in this major.
export default async function GiftPage(props: PageProps<"/gift/[token]">) {
  const { token } = await props.params;
  return <GiftClient token={token} />;
}
