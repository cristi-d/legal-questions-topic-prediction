document.querySelectorAll(".listing-forum > h5 > a").forEach(it => {
    const href = it.href;

    const idRegex = /^.+\/forum\/([0-9]+)\/.+/;
    const result = idRegex.exec(href);
    const id = result[1];

    console.log(`${id}, ${href}`);
});
